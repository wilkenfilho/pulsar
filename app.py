import io
import re
import time
from collections import Counter
from datetime import datetime
from itertools import combinations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

TRAC_ENDPOINT = "https://trac.pulsarplatform.com/graphql"
DATA_ENDPOINT_BASE = "https://data.pulsarplatform.com/graphql/trac"
DEFAULT_CATEGORIES = ["REDDIT", "FORUMS", "FACEBOOK"]

st.set_page_config(page_title="Radar Simples — Pulsar", page_icon="📡", layout="wide")

# ---------------- session defaults ----------------
for key, default in {
    "token": "",
    "categories": DEFAULT_CATEGORIES,
    "last_result": None,
    "searches": [],
    "results_field": None,
    "results_query": None,
    "results_columns": [],
    "results_df": None,
    "folder_id": "25",
    "folder_support": None,
    "waiting_for": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ---------------- low-level GraphQL ----------------
def gql(endpoint, query, variables=None):
    token = st.session_state.token
    if not token:
        raise RuntimeError("Cole seu token na aba Configurações antes de continuar.")
    try:
        resp = requests.post(
            endpoint,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=60,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Falha de rede ao chamar {endpoint}: {e}")

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"Resposta não-JSON (status {resp.status_code}): {resp.text[:300]}")

    if resp.status_code >= 400:
        msg = (data.get("errors") or [{}])[0].get("message", str(data))
        raise RuntimeError(f"HTTP {resp.status_code}: {msg}")
    if data.get("errors"):
        raise RuntimeError(" | ".join(e["message"] for e in data["errors"]))
    return data["data"]


def trac_gql(query, variables=None):
    return gql(TRAC_ENDPOINT, query, variables)


def data_gql(query, variables=None, search_hash=None):
    endpoint = f"{DATA_ENDPOINT_BASE}/{search_hash}" if search_hash else DATA_ENDPOINT_BASE
    return gql(endpoint, query, variables)


def friendly_error(msg: str) -> str:
    low = msg.lower()
    if "already been taken" in low:
        return (
            f"{msg} — já existe uma busca com esse nome no Pulsar. Marque a opção "
            '"adicionar sufixo automático" ou escolha outro nome.'
        )
    if "not available yet" in low or "not enabled" in low:
        return (
            f"{msg} — essa rede existe no schema da API mas ainda não está habilitada para a sua conta. "
            "Isso é uma liberação comercial separada; peça ao seu Account Manager do Pulsar para ativar essa fonte."
        )
    return msg


# ---------------- introspection helpers (TRAC metadata) ----------------
def unwrap_named_type(t):
    cur, depth = t, 0
    while cur and not cur.get("name") and cur.get("ofType") and depth < 8:
        cur = cur["ofType"]
        depth += 1
    return cur


def introspect_input_fields(type_name, endpoint=None):
    endpoint = endpoint or TRAC_ENDPOINT
    query = f"""
    query {{
      __type(name: "{type_name}") {{
        inputFields {{
          name
          type {{
            kind name
            ofType {{ kind name
              ofType {{ kind name
                ofType {{ kind name
                  ofType {{ kind name }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}"""
    data = gql(endpoint, query)
    return (data.get("__type") or {}).get("inputFields") or []


def detect_categories():
    input_fields = introspect_input_fields("CreateBooleanTopicsSearchInput")
    cat_field = next((f for f in input_fields if "categor" in f["name"].lower()), None)
    if not cat_field:
        raise RuntimeError("campo de categorias não encontrado no schema (introspection pode estar desabilitada).")
    named = unwrap_named_type(cat_field["type"])
    if not named or not named.get("name"):
        raise RuntimeError(f'não consegui resolver o tipo enum de "{cat_field["name"]}".')
    enum_query = f'query {{ __type(name: "{named["name"]}") {{ enumValues {{ name }} }} }}'
    enum_data = trac_gql(enum_query)
    values = [v["name"] for v in (enum_data.get("__type") or {}).get("enumValues") or []]
    if not values:
        raise RuntimeError(f'enum "{named["name"]}" retornou vazio.')

    # Plain "Instagram" doesn't actually work on this account — only the Instagram_Public
    # variant does. Drop the broken one whenever the working variant is also available.
    upper_values = [v.upper() for v in values]
    if "INSTAGRAM" in upper_values and any(v.upper().startswith("INSTAGRAM_PUBLIC") for v in values):
        values = [v for v in values if v.upper() != "INSTAGRAM"]

    return values


# ---------------- folder support (schema-driven, nothing hardcoded) ----------------
def introspect_mutation_fields(endpoint):
    query = """
    query {
      __schema {
        mutationType {
          fields {
            name
            args { name type { kind name ofType { kind name ofType { kind name } } } }
          }
        }
      }
    }"""
    data = gql(endpoint, query)
    mt = data["__schema"]["mutationType"]
    return mt["fields"] if mt else []


def detect_assign_mutation():
    """Look for any mutation that takes an `input: XInput!` object containing both an
    id-like field and a folder-like field — covers a dedicated moveToFolder as well as
    a general updateSearch/editSearch mutation, whichever the schema actually has."""
    mutation_fields = introspect_mutation_fields(TRAC_ENDPOINT)
    for f in mutation_fields:
        input_arg = next((a for a in f["args"] if a["name"] == "input"), None)
        if not input_arg:
            continue
        named = unwrap_named_type(input_arg["type"])
        if not named.get("name"):
            continue
        try:
            infields = introspect_input_fields(named["name"])
        except Exception:
            continue
        id_field = next((x["name"] for x in infields if x["name"].lower() in ("id", "searchid")), None)
        folder_field = next((x["name"] for x in infields if "folder" in x["name"].lower()), None)
        if id_field and folder_field:
            return {"mutation": f["name"], "input_type": named["name"], "id_field": id_field, "folder_field": folder_field}
    return None


def assign_to_folder(assign_info, search_id, folder_id):
    mutation = f"""
    mutation AssignFolder($input: {assign_info['input_type']}!){{
      {assign_info['mutation']}(input:$input){{ errors{{ id message extensions }} }}
    }}"""
    input_obj = {assign_info["id_field"]: search_id, assign_info["folder_field"]: folder_id}
    data = trac_gql(mutation, {"input": input_obj})
    result = data[assign_info["mutation"]]
    errors = result.get("errors")
    if errors:
        raise RuntimeError(" | ".join(friendly_error(e["message"]) for e in errors))


def detect_folder_support():
    """Ask the TRAC metadata schema, in five different places, whether folders exist:
    1) an arg on the `searches` query (e.g. folderId)
    2) a standalone top-level query field (e.g. `folder`/`folders`)
    3) a field on the Search type itself, for client-side filtering
    4) an input field on CreateBooleanTopicsSearchInput, to assign new searches to a folder
    5) a mutation (dedicated or general edit/update) that can move an existing search into a folder
    """
    info = {
        "searches_arg": None,
        "top_level_field": None,
        "search_type_folder_field": None,
        "create_input_field": None,
        "assign_mutation": None,
        "raw_query_fields": [],
    }
    fields = introspect_query_fields(TRAC_ENDPOINT)
    info["raw_query_fields"] = [f["name"] for f in fields]

    searches_field = next((f for f in fields if f["name"] == "searches"), None)
    if searches_field:
        for a in searches_field["args"]:
            if "folder" in a["name"].lower():
                info["searches_arg"] = a["name"]
                break

        named = unwrap_named_type(searches_field["type"])
        if named.get("name"):
            result_type = introspect_type_fields(TRAC_ENDPOINT, named["name"])
            nodes_field = next((f for f in (result_type.get("fields") or []) if f["name"] == "nodes"), None)
            if nodes_field:
                node_named = unwrap_named_type(nodes_field["type"])
                if node_named.get("name"):
                    node_type = introspect_type_fields(TRAC_ENDPOINT, node_named["name"])
                    folder_field = next(
                        (f for f in (node_type.get("fields") or []) if "folder" in f["name"].lower()), None
                    )
                    if folder_field:
                        info["search_type_folder_field"] = folder_field["name"]

    top_field = next((f for f in fields if "folder" in f["name"].lower()), None)
    if top_field:
        info["top_level_field"] = top_field["name"]

    create_fields = introspect_input_fields("CreateBooleanTopicsSearchInput")
    folder_input = next((f for f in create_fields if "folder" in f["name"].lower()), None)
    if folder_input:
        info["create_input_field"] = folder_input["name"]

    try:
        info["assign_mutation"] = detect_assign_mutation()
    except Exception:
        info["assign_mutation"] = None

    return info


def enrich_names(nodes):
    """The dedicated folder field's node type may not expose 'name' at all. Cross-reference
    with the documented `searches` query (which always has name) by hash, falling back to id."""
    if not nodes or all(n.get("name") for n in nodes):
        return nodes
    try:
        # Force unfiltered call — the folder-filtered path may also lack names
        all_searches = list_searches(folder_support=None, folder_id=None)
        by_hash = {s.get("searchHash"): s.get("name") for s in all_searches if s.get("searchHash") and s.get("name")}
        by_id = {str(s.get("id")): s.get("name") for s in all_searches if s.get("id") is not None and s.get("name")}
    except Exception:
        return nodes
    for n in nodes:
        if not n.get("name"):
            n["name"] = (
                by_hash.get(n.get("searchHash"))
                or by_id.get(str(n.get("id")))
                or n.get("title")
                or n.get("label")
            )
    return nodes


def fetch_folder_searches(folder_field_name, folder_id):
    """Follow Query.<folder_field_name>(id) -> find its nested searches-like connection -> flatten to a list of dicts."""
    fields = introspect_query_fields(TRAC_ENDPOINT)
    folder_field = next(f for f in fields if f["name"] == folder_field_name)
    id_arg = next((a["name"] for a in folder_field["args"] if a["name"].lower() in ("id", "folderid")), None)
    if not id_arg:
        id_arg = folder_field["args"][0]["name"] if folder_field["args"] else "id"

    folder_named = unwrap_named_type(folder_field["type"])
    folder_type = introspect_type_fields(TRAC_ENDPOINT, folder_named["name"])
    folder_fields = folder_type.get("fields") or []

    conn_field = next(
        (f for f in folder_fields if any(k in f["name"].lower() for k in ("search", "topic", "item", "content"))),
        None,
    )
    if not conn_field:
        raise RuntimeError(
            f'o tipo "{folder_named["name"]}" não expõe um campo óbvio de buscas '
            f'(campos disponíveis: {", ".join(f["name"] for f in folder_fields)}).'
        )

    conn_named = unwrap_named_type(conn_field["type"])
    conn_type = introspect_type_fields(TRAC_ENDPOINT, conn_named["name"]) if conn_named.get("name") else {}
    conn_type_fields = conn_type.get("fields") or []
    nodes_field = next((f for f in conn_type_fields if f["name"] in ("nodes", "edges", "items", "results")), None)

    preferred = {"id", "name", "title", "label", "totalContents", "startDate", "status", "searchHash", "realtimeStatus"}

    if nodes_field:
        node_named = unwrap_named_type(nodes_field["type"])
        node_type = introspect_type_fields(TRAC_ENDPOINT, node_named["name"])
        node_fields = node_type.get("fields") or []
        leaf_names = [f["name"] for f in node_fields if unwrap_named_type(f["type"]).get("kind") in LEAF_KINDS]
        selection_fields = [n for n in leaf_names if n in preferred] or leaf_names[:10]
        selection = "\n            ".join(selection_fields)
        query = f"""
        query FolderSearches($id: ID!) {{
          {folder_field_name}({id_arg}: $id) {{
            {conn_field["name"]} {{
              {nodes_field["name"]} {{
                {selection}
              }}
            }}
          }}
        }}"""
        data = trac_gql(query, {"id": str(folder_id)})
        root = data[folder_field_name][conn_field["name"]][nodes_field["name"]]
        if nodes_field["name"] == "edges":
            root = [e["node"] for e in root]
        return enrich_names(root)
    else:
        leaf_names = [f["name"] for f in conn_type_fields if unwrap_named_type(f["type"]).get("kind") in LEAF_KINDS]
        selection_fields = [n for n in leaf_names if n in preferred] or leaf_names[:10]
        selection = "\n          ".join(selection_fields)
        query = f"""
        query FolderSearches($id: ID!) {{
          {folder_field_name}({id_arg}: $id) {{
            {conn_field["name"]} {{
              {selection}
            }}
          }}
        }}"""
        data = trac_gql(query, {"id": str(folder_id)})
        root = data[folder_field_name][conn_field["name"]]
        root = root if isinstance(root, list) else [root]
        return enrich_names(root)


# ---------------- TRAC metadata mutations ----------------
def create_search(name, categories, boolean_expr, folder_input_field=None, folder_id=None, facebook_keywords=None):
    input_obj = {"name": name, "categories": categories, "booleanExpression": boolean_expr}
    if folder_input_field and folder_id:
        input_obj[folder_input_field] = folder_id
    if facebook_keywords:
        input_obj["facebookKeywords"] = facebook_keywords
    mutation = """
    mutation CreateBoolTopic($input: CreateBooleanTopicsSearchInput!){
      createBooleanTopicsSearch(input:$input){
        search{ id name searchHash }
        errors{ id message extensions }
      }
    }"""
    data = trac_gql(mutation, {"input": input_obj})
    result = data["createBooleanTopicsSearch"]
    if result.get("errors"):
        raise RuntimeError(" | ".join(friendly_error(e["message"]) for e in result["errors"]))
    return result["search"]


def create_historic(search_id, categories, start_iso, end_iso):
    mutation = """
    mutation CreateHistoric($input: CreateHistoricInput!){
      createHistoric(input:$input){ errors{ id message extensions } }
    }"""
    data = trac_gql(mutation, {"input": {"searchId": search_id, "categories": categories,
                                          "startDate": start_iso, "endDate": end_iso}})
    errors = data["createHistoric"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(friendly_error(e["message"]) for e in errors))


def get_historic_ids(search_id):
    query = """
    query PreviewHistorics($sid: ID){
      historics(searchId:$sid){ nodes{ id status } }
    }"""
    data = trac_gql(query, {"sid": search_id})
    return [n["id"] for n in data["historics"]["nodes"]]


def get_historics_progress(search_id, historic_ids):
    query = """
    query PreviewHistorics($sid: ID){
      historics(searchId:$sid){ nodes{ id status progress } }
    }"""
    data = trac_gql(query, {"sid": search_id})
    nodes = [n for n in data["historics"]["nodes"] if n["id"] in historic_ids]
    if not nodes:
        return None, []
    raw_values = [n.get("progress") for n in nodes if n.get("progress") is not None]
    if not raw_values:
        return None, [n.get("status") for n in nodes]
    # normalize: API may return 0-1 fraction or 0-100 percentage
    scaled = [v * 100 if v <= 1 else v for v in raw_values]
    avg_progress = sum(scaled) / len(scaled)
    return avg_progress, [n.get("status") for n in nodes]


TERMINAL_FAIL = ("fail", "error", "cancelled", "canceled")


def run_full_historic_flow(placeholder, search, historic_ids, max_wait_preview=300, max_wait_collect=7200, interval=8, fetch_check_every=20):
    """One continuous, automatic wait — no clicks, no reruns:
    1) waits for the preview to finish computing (the '29K results' count in the Pulsar UI),
    2) launches the historic itself the moment it's ready (the 'rocket'),
    3) keeps watching the real post-launch progress (with an ETA estimated from its own trend)
       while periodically probing the Data Endpoint for real fetchable results — a non-empty
       fetch is the actual ground truth for 'done', the status text is just shown for context.
    Returns ("done", df, used_field) / ("failed"|"launch_failed", message, None) /
    ("long_wait", attempts, last_status_text).
    """
    started = time.time()

    # Phase 1 — preview
    while True:
        elapsed = time.time() - started
        try:
            progress, statuses = get_historics_progress(search["id"], historic_ids)
        except Exception as e:
            progress, statuses = None, [str(e)]

        statuses_low = [str(s).lower() for s in statuses if s]
        if any(any(k in s for k in TERMINAL_FAIL) for s in statuses_low):
            return "failed", ", ".join(set(str(s) for s in statuses)), None

        with placeholder.container():
            st.progress(min(int(progress or 0), 100) / 100)
            st.caption(
                f"Calculando preview: {progress:.0f}% · status: {', '.join(set(str(s) for s in statuses)) or '—'}"
                if progress is not None else f"Preparando preview... ({int(elapsed)}s)"
            )

        if progress is not None and progress >= 99:
            break
        if elapsed > max_wait_preview:
            break  # generous wait already given — proceed to launch attempt anyway
        time.sleep(interval)

    # Launch (the "rocket")
    with placeholder.container():
        st.caption("🚀 Preview pronto — lançando o histórico automaticamente...")
    try:
        launch_historic(historic_ids)
    except Exception as e:
        return "launch_failed", str(e), None

    # Phase 2 — collecting, with ETA, checking the Data Endpoint periodically without stopping the loop
    started2 = time.time()
    samples = []
    last_fetch_check = -fetch_check_every
    last_attempts = []
    last_status_text = "—"
    debug_placeholder = st.empty()
    looks_done_streak = 0
    LOOKS_DONE_HINTS = ("complet", "done", "finish", "ready", "success")

    while True:
        elapsed2 = time.time() - started2
        try:
            progress2, statuses2 = get_historics_progress(search["id"], historic_ids)
        except Exception:
            progress2, statuses2 = None, []
        if statuses2:
            last_status_text = ", ".join(set(str(s) for s in statuses2))

        eta_txt = ""
        if progress2 is not None:
            samples.append((elapsed2, progress2))
            if len(samples) >= 2:
                t0, p0 = samples[0]
                rate = (progress2 - p0) / max(elapsed2 - t0, 1e-6)
                if rate > 0.01 and progress2 < 100:
                    eta_sec = (100 - progress2) / rate
                    mins = int(eta_sec // 60)
                    eta_txt = f" · ETA ~{mins} min" if mins >= 1 else " · ETA menos de 1 min"

        with placeholder.container():
            if progress2 is not None:
                st.progress(min(int(progress2), 100) / 100)
                st.caption(f"Coletando: {progress2:.0f}%{eta_txt} · status: {last_status_text} · {int(elapsed2)}s decorridos")
            else:
                st.caption(f"Aguardando a coleta começar... ({int(elapsed2)}s decorridos)")

        # the real Data Endpoint probe is heavier (several GraphQL calls) — only do it every N seconds,
        # while the lightweight progress/ETA display above keeps updating every `interval`
        if elapsed2 - last_fetch_check >= fetch_check_every:
            last_fetch_check = elapsed2
            try:
                df, used_field, attempts = fetch_all_results_auto(search)
            except Exception:
                df, used_field, attempts = pd.DataFrame(), None, []
            last_attempts = attempts
            if not df.empty:
                debug_placeholder.empty()
                return "done", df, used_field

            # Pulsar says it's done (COMPLETED/READY/etc.) but the Data Endpoint still won't
            # give us anything — surface that live instead of silently retrying for up to 2h.
            status_looks_done = any(h in last_status_text.lower() for h in LOOKS_DONE_HINTS)
            looks_done_streak = looks_done_streak + 1 if status_looks_done else 0
            if looks_done_streak >= 2:
                with debug_placeholder.container():
                    st.warning(
                        f'O Pulsar reporta status "{last_status_text}" (parece concluído), mas o Data Endpoint '
                        "ainda não devolveu registros para nenhum campo testado. Continuando a tentar automaticamente "
                        "— pode ser só um atraso de sincronização entre os dois serviços."
                    )
                    render_fetch_attempts(attempts)
            else:
                debug_placeholder.empty()

        if elapsed2 > max_wait_collect:
            return "long_wait", last_attempts, last_status_text
        time.sleep(interval)






def launch_historic(ids):
    mutation = """
    mutation launchHistoric($input: LaunchHistoricInput!){
      launchHistoric(input:$input){ errors{ id message extensions } }
    }"""
    data = trac_gql(mutation, {"input": {"ids": ids}})
    errors = data["launchHistoric"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(friendly_error(e["message"]) for e in errors))


def start_realtime(search_id, start_iso=None, end_iso=None):
    mutation = """
    mutation startSearch($input: StartSearchInput!){
      startSearch(input:$input){ errors{ id message extensions } }
    }"""
    inp = {"id": search_id}
    if start_iso:
        inp["startDate"] = start_iso
    if end_iso:
        inp["endDate"] = end_iso
    data = trac_gql(mutation, {"input": inp})
    errors = data["startSearch"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(friendly_error(e["message"]) for e in errors))


def stop_search(search_id):
    mutation = """
    mutation stopSearch($input: StopSearchInput!){
      stopSearch(input:$input){ errors{ id message extensions } }
    }"""
    data = trac_gql(mutation, {"input": {"id": search_id}})
    errors = data["stopSearch"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(friendly_error(e["message"]) for e in errors))


def list_searches(folder_support=None, folder_id=None):
    var_defs = "$sortBy: AllowedFieldsForSorting"
    args = "sortBy:$sortBy"
    variables = {"sortBy": "NAME"}
    extra_field = ""

    if folder_support and folder_support.get("searches_arg") and folder_id:
        arg_name = folder_support["searches_arg"]
        var_defs += f", ${arg_name}: ID"
        args += f", {arg_name}:${arg_name}"
        variables[arg_name] = str(folder_id)
    elif folder_support and folder_support.get("search_type_folder_field"):
        extra_field = folder_support["search_type_folder_field"]

    query = f"""
    query Searches({var_defs}){{
      searches({args}){{
        totalCount
        nodes{{ id name totalContents startDate status searchHash realtimeStatus {extra_field} }}
      }}
    }}"""
    # The API defaults to page 1 with 10 results. Fetch multiple pages to get all searches.
    all_nodes = []
    page = 1
    while True:
        page_vars = {**variables, "page": page}
        page_query = f"""
        query Searches({var_defs}, $page: Int){{
          searches({args}, page:$page){{
            totalCount
            nodes{{ id name totalContents startDate status searchHash realtimeStatus {extra_field} }}
          }}
        }}"""
        try:
            data = trac_gql(page_query, page_vars)
        except Exception:
            # Some schemas may not accept `page` arg — fall back to single page
            data = trac_gql(query, variables)
            all_nodes = data["searches"]["nodes"]
            break
        nodes = data["searches"]["nodes"]
        all_nodes.extend(nodes)
        total = data["searches"].get("totalCount", 0)
        if len(all_nodes) >= total or len(nodes) == 0 or page > 50:
            break
        page += 1
    nodes = all_nodes

    if (
        folder_id
        and folder_support
        and not folder_support.get("searches_arg")
        and folder_support.get("search_type_folder_field")
    ):
        key = folder_support["search_type_folder_field"]
        nodes = [n for n in nodes if str(n.get(key)) == str(folder_id)]

    return nodes


def iso_start(d):
    return f"{d.isoformat()}T00:00:00Z" if d else None


def iso_end(d):
    return f"{d.isoformat()}T23:59:59Z" if d else None


# ---------------- advanced boolean operators (TRAC Full Boolean Operator Guide) ----------------
def build_boolean_expression(base_expr, adv):
    """Wrap the user's plain-language boolean and silently AND-in the advanced operators
    the person picked in 'Configurações avançadas' — they never see this raw syntax."""
    parts = [f"({base_expr.strip()})"] if base_expr.strip() else []

    if adv.get("not_rt"):
        parts.append("NOT RT")
    if adv.get("not_quote"):
        parts.append("NOT QUOTE")
    if adv.get("not_reply"):
        parts.append("NOT REPLY")

    rt_of_exclude = [h.strip() for h in adv.get("not_rt_of", "").split(",") if h.strip()]
    if rt_of_exclude:
        parts.append(f"NOT RT_OF({' OR '.join(_at(h) for h in rt_of_exclude)})")

    by_twitter = [h.strip() for h in adv.get("by_twitter", "").split(",") if h.strip()]
    if by_twitter:
        parts.append(f"BY_TWITTER({' OR '.join(_at(h) for h in by_twitter)})")

    not_by_twitter = [h.strip() for h in adv.get("not_by_twitter", "").split(",") if h.strip()]
    if not_by_twitter:
        parts.append(f"NOT BY_TWITTER({' OR '.join(_at(h) for h in not_by_twitter)})")

    sites = [s.strip() for s in adv.get("not_site", "").split(",") if s.strip()]
    if sites:
        parts.append(f"NOT SITE({' OR '.join(sites)})")

    langs = [l.strip() for l in adv.get("langs", "").split(",") if l.strip()]
    if langs:
        parts.append(f"LANG({' OR '.join(langs)})")

    if adv.get("location"):
        parts.append(f"LOCATION({adv['location'].strip()})")

    media = adv.get("media") or []
    if media:
        parts.append(f"MEDIA({' OR '.join(media)})")

    if adv.get("sample"):
        parts.append(f"SAMPLE {int(adv['sample'])}")

    if adv.get("pinterest"):
        parts.append(f"PINTEREST ({base_expr.strip()})")
    if adv.get("twitch"):
        parts.append(f"TWITCH ({base_expr.strip()})")

    if adv.get("extra"):
        parts.append(adv["extra"].strip())

    return " AND ".join(p for p in parts if p)


def _at(handle):
    handle = handle.strip()
    return handle if handle.startswith("@") else f"@{handle}"


# ---------------- Data Endpoint discovery (introspection-driven, no guessed field names) ----------------
def introspect_query_fields(endpoint):
    query = """
    query {
      __schema {
        queryType {
          fields {
            name
            args { name type { kind name ofType { kind name ofType { kind name } } } }
            type {
              kind name
              ofType { kind name ofType { kind name ofType { kind name } } }
            }
          }
        }
      }
    }"""
    data = gql(endpoint, query)
    return data["__schema"]["queryType"]["fields"]


def introspect_type_fields(endpoint, type_name):
    query = f"""
    query {{
      __type(name: "{type_name}") {{
        name
        kind
        fields {{
          name
          type {{
            kind name
            ofType {{ kind name ofType {{ kind name ofType {{ kind name }} }} }}
          }}
        }}
      }}
    }}"""
    data = gql(endpoint, query)
    return data["__type"]


LEAF_KINDS = {"SCALAR", "ENUM"}
BAD_FIELD_HINTS = ("check", "shareable", "share", "validate", "verify", "exists")
GOOD_FIELD_HINTS = ("content", "post", "result", "mention", "message", "trac")
SEARCH_ARG_HINTS = ("searchhash", "searchid", "hash", "topicid")


def score_results_field(endpoint, f):
    """Rank a Query field by how likely it is to be 'give me all the posts for a search',
    as opposed to a single-item lookup like checkShareableContent(contentId)."""
    name = f["name"].lower()
    arg_names = [a["name"].lower() for a in f["args"]]
    score = 0

    if any(k in name for k in GOOD_FIELD_HINTS):
        score += 3
    if any(k in name for k in BAD_FIELD_HINTS):
        score -= 6
    if any(any(h in a for h in SEARCH_ARG_HINTS) for a in arg_names):
        score += 3
    if len(f["args"]) == 1 and "content" in arg_names[0] and "id" in arg_names[0]:
        score -= 4  # single-content-by-id lookup pattern, not a bulk listing

    try:
        named = unwrap_named_type(f["type"])
        if f["type"].get("kind") == "LIST" or (named.get("kind") == "OBJECT"):
            result_type = introspect_type_fields(endpoint, named["name"]) if named.get("name") else {}
            rfields = [x["name"] for x in (result_type.get("fields") or [])]
            if any(x in rfields for x in ("nodes", "edges", "totalCount", "items", "results")):
                score += 4
            elif rfields:
                score -= 2  # looks like a single flat object, not a collection
    except Exception:
        pass

    return score


def rank_results_fields(endpoint, fields):
    candidates = [f for f in fields if any(k in f["name"].lower() for k in GOOD_FIELD_HINTS)]
    if not candidates:
        candidates = fields
    scored = [(score_results_field(endpoint, f), f) for f in candidates]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [f for _, f in scored]


ID_HINTS_RE = ("hash", "search", "topic")


def _looks_like_identifier(name_lower):
    if any(h in name_lower for h in ID_HINTS_RE):
        return True
    return name_lower in ("id", "ids") or name_lower.endswith("id") or name_lower.endswith("ids")


def pick_identifier_arg(field_info, search):
    """Direct scalar/enum arg on the field itself that looks like it wants the search's id/hash."""
    for a in field_info["args"]:
        low = a["name"].lower()
        if "hash" in low:
            return a["name"], search["searchHash"]
    for a in field_info["args"]:
        low = a["name"].lower()
        if _looks_like_identifier(low):
            return a["name"], search["id"]
    return None, None


def resolve_identifier(endpoint, field_info, search):
    """Find how to pass the search's id/hash to this field: either a direct scalar arg,
    or a field nested inside an INPUT_OBJECT arg (a common 'filter: {...}' pattern).
    Returns (arg_name, gql_type, value, nested_field_name_or_None).
    """
    arg_name, value = pick_identifier_arg(field_info, search)
    if arg_name:
        for a in field_info["args"]:
            if a["name"] == arg_name:
                named = unwrap_named_type(a["type"])
                gql_type = (named.get("name") or "ID") + ("!" if a["type"].get("kind") == "NON_NULL" else "")
                return arg_name, gql_type, value, None

    for a in field_info["args"]:
        named = unwrap_named_type(a["type"])
        if named.get("kind") == "INPUT_OBJECT" and named.get("name"):
            try:
                input_fields = introspect_input_fields(named["name"], endpoint=endpoint)
            except Exception:
                continue
            for f in input_fields:
                low = f["name"].lower()
                if "hash" in low:
                    gql_type = named["name"] + ("!" if a["type"].get("kind") == "NON_NULL" else "")
                    return a["name"], gql_type, {f["name"]: search["searchHash"]}, named["name"]
            for f in input_fields:
                low = f["name"].lower()
                if _looks_like_identifier(low):
                    gql_type = named["name"] + ("!" if a["type"].get("kind") == "NON_NULL" else "")
                    return a["name"], gql_type, {f["name"]: search["id"]}, named["name"]
    return None, None, None, None


def build_dynamic_query(endpoint, field_name, arg_values, field_cap=300):
    field_info = next(f for f in introspect_query_fields(endpoint) if f["name"] == field_name)
    return_named = unwrap_named_type(field_info["type"])
    result_type = introspect_type_fields(endpoint, return_named["name"])

    nodes_field = next((f for f in (result_type.get("fields") or []) if f["name"] in ("nodes", "edges", "items", "results")), None)

    if nodes_field:
        node_named = unwrap_named_type(nodes_field["type"])
        if node_named["name"] and (result_type.get("kind") != "SCALAR"):
            inner_type = introspect_type_fields(endpoint, node_named["name"])
            inner_fields = inner_type.get("fields") or []
            has_node = next((f for f in inner_fields if f["name"] == "node"), None)
            if has_node and nodes_field["name"] == "edges":
                node_named = unwrap_named_type(has_node["type"])
                inner_type = introspect_type_fields(endpoint, node_named["name"])
                inner_fields = inner_type.get("fields") or []
        else:
            inner_fields = []
        leaf_fields = [f["name"] for f in inner_fields if unwrap_named_type(f["type"]).get("kind") in LEAF_KINDS]
        leaf_fields = leaf_fields[:field_cap] or ["id"]
        args_str = ", ".join(f"{k}: ${k}" for k in arg_values.keys())
        var_defs = ", ".join(f"${k}: {v['gql_type']}" for k, v in arg_values.items())
        selection = "\n      ".join(leaf_fields)
        query = f"""
        query DynamicResults({var_defs}) {{
          {field_name}({args_str}) {{
            {"totalCount" if any(f["name"]=="totalCount" for f in (result_type.get("fields") or [])) else ""}
            {nodes_field["name"]} {{
              {"node { " + selection + " }" if nodes_field["name"] == "edges" else selection}
            }}
          }}
        }}"""
        return query, nodes_field["name"]
    else:
        leaf_fields = [f["name"] for f in (result_type.get("fields") or []) if unwrap_named_type(f["type"]).get("kind") in LEAF_KINDS]
        leaf_fields = leaf_fields[:field_cap] or ["id"]
        args_str = ", ".join(f"{k}: ${k}" for k in arg_values.keys())
        var_defs = ", ".join(f"${k}: {v['gql_type']}" for k, v in arg_values.items())
        selection = "\n      ".join(leaf_fields)
        query = f"""
        query DynamicResults({var_defs}) {{
          {field_name}({args_str}) {{
            {selection}
          }}
        }}"""
        return query, None


def flatten_results(payload, field_name, nodes_key):
    root = payload[field_name]
    if nodes_key:
        items = root[nodes_key]
        if nodes_key == "edges":
            items = [e["node"] for e in items]
    else:
        items = root if isinstance(root, list) else [root]
    return pd.DataFrame(items)


def introspect_filter_input(endpoint):
    """Get ALL fields of FilterInput so we can see exactly what the Data Endpoint expects."""
    try:
        fields = introspect_input_fields("FilterInput", endpoint=endpoint)
        return {f["name"]: unwrap_named_type(f["type"]).get("name", "?") for f in fields}
    except Exception:
        return {}


def build_filter_candidates(search, filter_fields):
    """Given the known fields of FilterInput and the search object, generate every plausible
    filter dict to try — from most likely to least likely."""
    hash_val = search.get("searchHash") or search.get("hash")
    id_val = search.get("id")
    candidates = []

    # Priority 1: fields that scream "this is where the search hash goes"
    for field_name in filter_fields:
        low = field_name.lower()
        if "hash" in low or low == "search" or low == "searchhash":
            if hash_val:
                candidates.append({field_name: hash_val})

    # Priority 2: same fields but with the numeric ID (some APIs accept both)
    for field_name in filter_fields:
        low = field_name.lower()
        if "hash" in low or low == "search":
            if id_val:
                candidates.append({field_name: str(id_val)})

    # Priority 3: ID-like fields with the numeric id
    for field_name in filter_fields:
        low = field_name.lower()
        if low in ("id", "searchid", "topicid") or low.endswith("id"):
            if id_val:
                candidates.append({field_name: int(id_val) if str(id_val).isdigit() else id_val})

    # Priority 4: ID-like fields with the hash (some APIs are flexible)
    for field_name in filter_fields:
        low = field_name.lower()
        if low in ("id", "searchid"):
            if hash_val:
                candidates.append({field_name: hash_val})

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for c in candidates:
        key = str(sorted(c.items()))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def fetch_all_results_auto(search):
    """The Data Endpoint is scoped by search hash IN THE URL:
    https://data.pulsarplatform.com/graphql/trac/{searchHash}
    FilterInput is only for post-fetch filtering (date range, sentiment, etc.), not for
    identifying the search. This was the root cause of every 'Searches not found' error."""
    search_hash = search.get("searchHash") or search.get("hash")
    if not search_hash:
        return pd.DataFrame(), None, [{"field": "(erro)", "message": "objeto de busca sem searchHash", "query": None, "variables": search}]

    endpoint = f"{DATA_ENDPOINT_BASE}/{search_hash}"
    attempts = []

    try:
        fields = introspect_query_fields(endpoint)
    except Exception as e:
        attempts.append({"field": "(introspection)", "message": f"falha ao introspectar {endpoint}: {e}", "query": None, "variables": None})
        return pd.DataFrame(), None, attempts

    ranked = rank_results_fields(endpoint, fields)
    if not ranked:
        attempts.append({"field": "(diagnóstico)", "message": f"nenhum campo candidato encontrado. Campos disponíveis: {[f['name'] for f in fields]}", "query": None, "variables": None})
        return pd.DataFrame(), None, attempts

    for field_info in ranked[:5]:
        # Find if this field has a FilterInput arg (optional — pass empty or skip)
        filter_arg = None
        filter_type = None
        for a in field_info["args"]:
            named = unwrap_named_type(a["type"])
            if named.get("kind") == "INPUT_OBJECT":
                filter_arg = a["name"]
                filter_type = named["name"] + ("!" if a["type"].get("kind") == "NON_NULL" else "")
                break

        # Build query — with or without the filter arg
        if filter_arg and filter_type:
            arg_values = {filter_arg: {"value": {}, "gql_type": filter_type}}
        else:
            arg_values = {}

        query, nodes_key = None, None
        try:
            query, nodes_key = build_dynamic_query(endpoint, field_info["name"], arg_values)
            variables = {k: v["value"] for k, v in arg_values.items()} if arg_values else {}
            payload = data_gql(query, variables, search_hash=search_hash)
            df = flatten_results(payload, field_info["name"], nodes_key)
            if not df.empty:
                return df, field_info["name"], attempts
            attempts.append({"field": field_info["name"], "message": "retornou 0 registros", "query": query, "variables": variables})
        except Exception as e:
            err_msg = str(e)
            # If FilterInput is required but we sent empty, try without the arg entirely
            if filter_arg and "required" in err_msg.lower():
                try:
                    arg_values2 = {}
                    query2, nodes_key2 = build_dynamic_query(endpoint, field_info["name"], arg_values2)
                    payload2 = data_gql(query2, {}, search_hash=search_hash)
                    df2 = flatten_results(payload2, field_info["name"], nodes_key2)
                    if not df2.empty:
                        return df2, field_info["name"], attempts
                except Exception as e2:
                    err_msg = f"{err_msg} | sem filtro: {e2}"
            attempts.append({"field": field_info["name"], "message": err_msg, "query": query, "variables": {k: v["value"] for k, v in arg_values.items()} if arg_values else {}})

    attempts.append({
        "field": "(diagnóstico)",
        "message": f"Endpoint usado: {endpoint}",
        "query": None,
        "variables": {"search_hash": search_hash, "fields_tried": [f["name"] for f in ranked[:5]]},
    })
    return pd.DataFrame(), None, attempts


def render_fetch_attempts(attempts):
    for a in attempts:
        with st.expander(f"**{a['field']}** — {a['message']}"):
            if a.get("query"):
                st.code(a["query"], language="graphql")
            if a.get("variables") is not None:
                st.json(a["variables"])
    st.caption("Copie isso (não o token) e me manda que eu ajusto a lógica de escolha de campo/argumento com precisão.")


# ---------------- text mining helpers (for word cloud / emoji / correlation) ----------------
STOPWORDS = set("""
a o os as de da do das dos em na no nas nos um uma uns umas para por com sem que quem qual
como mais menos muito pouco isso isto aquilo esse essa esses essas este esta estes estas
eu tu ele ela nos vos eles elas meu minha teu tua seu sua nosso nossa
e ou mas se nao não sim ja já ao aos à às pra pro num numa
the a an of to in on for with is are was were be been being this that these those
and or but not you your yours i my mine we our ours they their theirs it its
rt via http https com www co t co
""".split())

EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]",
    flags=re.UNICODE,
)
TOKEN_PATTERN = re.compile(r"[A-Za-zÀ-ÿ]{3,}")


def clean_tokens(text):
    text = re.sub(r"http\S+|www\.\S+", " ", str(text))
    text = re.sub(r"@\w+", " ", text)
    tokens = [t.lower() for t in TOKEN_PATTERN.findall(text)]
    return [t for t in tokens if t not in STOPWORDS]


def extract_emojis(text):
    return EMOJI_PATTERN.findall(str(text))


def render_dashboard(df, key_prefix=""):
    st.dataframe(df, use_container_width=True)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="resultados")
    st.download_button("⬇️ Baixar como Excel", buf.getvalue(), file_name="pulsar_resultados.xlsx", key=f"{key_prefix}dl")

    st.divider()
    st.subheader("Mapear colunas para os gráficos")
    cols = list(df.columns)

    def guess(keywords, default_idx=0):
        for kw in keywords:
            for c in cols:
                if kw in c.lower():
                    return c
        return cols[default_idx] if cols else None

    c1, c2, c3, c4 = st.columns(4)
    text_col = c1.selectbox("Coluna de texto", cols, index=cols.index(guess(["text", "content", "body", "message"])) if guess(["text", "content", "body", "message"]) in cols else 0, key=f"{key_prefix}text_col")
    date_col = c2.selectbox("Coluna de data", cols, index=cols.index(guess(["date", "created", "time", "publish"])) if guess(["date", "created", "time", "publish"]) in cols else 0, key=f"{key_prefix}date_col")
    sentiment_col = c3.selectbox("Coluna de sentimento (opcional)", ["(nenhuma)"] + cols, key=f"{key_prefix}sentiment_col")
    network_col = c4.selectbox("Coluna de rede (opcional)", ["(nenhuma)"] + cols, key=f"{key_prefix}network_col")

    st.divider()
    st.subheader("Dashboard")

    g1, g2 = st.columns(2)

    with g1:
        st.markdown("**Volume por período**")
        try:
            dts = pd.to_datetime(df[date_col], errors="coerce")
            vol = dts.dt.date.value_counts().sort_index()
            fig = px.line(x=vol.index, y=vol.values, labels={"x": "Data", "y": "Menções"})
            st.plotly_chart(fig, use_container_width=True)
        except Exception as e:
            st.warning(f"Não consegui montar o gráfico de linha: {e}")

    with g2:
        if sentiment_col != "(nenhuma)":
            st.markdown("**Sentimento**")
            try:
                vc = df[sentiment_col].astype(str).value_counts()
                fig = px.pie(names=vc.index, values=vc.values)
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.warning(f"Não consegui montar o gráfico de sentimento: {e}")
        else:
            st.info("Selecione uma coluna de sentimento acima para ver este gráfico.")

    all_tokens = []
    all_emojis = []
    for txt in df[text_col].dropna().astype(str):
        all_tokens.extend(clean_tokens(txt))
        all_emojis.extend(extract_emojis(txt))

    g3, g4 = st.columns(2)
    with g3:
        st.markdown("**Nuvem de palavras**")
        try:
            from wordcloud import WordCloud
            freq = Counter(all_tokens)
            if freq:
                wc = WordCloud(width=800, height=400, background_color="white").generate_from_frequencies(freq)
                st.image(wc.to_image(), use_container_width=True)
            else:
                st.info("Sem termos suficientes para gerar a nuvem.")
        except Exception as e:
            st.warning(f"Não consegui gerar a nuvem de palavras: {e}")

    with g4:
        st.markdown("**Nuvem de emoji**")
        freq_emoji = Counter(all_emojis)
        if freq_emoji:
            top = freq_emoji.most_common(30)
            fig = px.treemap(
                names=[e for e, _ in top],
                parents=[""] * len(top),
                values=[c for _, c in top],
            )
            fig.update_traces(textinfo="label+value", textfont_size=24)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Nenhum emoji encontrado no texto.")

    st.markdown("**Teia de termos correlacionados**")
    try:
        import networkx as nx

        top_terms = [t for t, _ in Counter(all_tokens).most_common(25)]
        cooc = Counter()
        for txt in df[text_col].dropna().astype(str):
            present = set(clean_tokens(txt)) & set(top_terms)
            for a, b in combinations(sorted(present), 2):
                cooc[(a, b)] += 1

        G = nx.Graph()
        for term in top_terms:
            G.add_node(term)
        for (a, b), w in cooc.items():
            if w > 0:
                G.add_edge(a, b, weight=w)

        if G.number_of_edges() > 0:
            pos = nx.spring_layout(G, seed=42, k=0.6)
            edge_x, edge_y = [], []
            for a, b in G.edges():
                edge_x += [pos[a][0], pos[b][0], None]
                edge_y += [pos[a][1], pos[b][1], None]
            edge_trace = go.Scatter(x=edge_x, y=edge_y, mode="lines", line=dict(width=1, color="#999"), hoverinfo="none")
            freq_all = Counter(all_tokens)
            node_x = [pos[n][0] for n in G.nodes()]
            node_y = [pos[n][1] for n in G.nodes()]
            node_size = [10 + freq_all[n] * 2 for n in G.nodes()]
            node_trace = go.Scatter(
                x=node_x, y=node_y, mode="markers+text", text=list(G.nodes()),
                textposition="top center", marker=dict(size=node_size, color="#FFB74A"),
            )
            fig = go.Figure(data=[edge_trace, node_trace])
            fig.update_layout(showlegend=False, xaxis=dict(visible=False), yaxis=dict(visible=False), height=550)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Termos não co-ocorreram o suficiente para montar a teia.")
    except Exception as e:
        st.warning(f"Não consegui montar a teia de termos: {e}")


# ---------------- UI ----------------
st.title("📡 Radar Simples — Pulsar")
st.caption("Boolean + redes + período → busca criada, histórico lançado, e resultados com dashboard — tudo por aqui.")

tab_config, tab_new, tab_list, tab_results = st.tabs(
    ["⚙️ Configurações", "🔍 Nova busca", "📋 Minhas buscas", "📊 Resultados"]
)

# ---- Configurações ----
with tab_config:
    st.subheader("Token de autorização")
    st.session_state.token = st.text_input(
        "Bearer Token",
        value=st.session_state.token,
        type="password",
        help="Emitido pelo seu Account Manager do Pulsar. Fica só na sessão deste app, em memória.",
    )
    st.divider()
    st.subheader("Redes disponíveis")
    st.caption(
        "A documentação pública só confirma REDDIT, FORUMS e FACEBOOK como exemplos. "
        "Detecte a lista real do seu schema — mas atenção: o enum pode listar uma rede que existe "
        "tecnicamente e ainda não estar habilitada comercialmente para sua conta (ex: Instagram)."
    )
    if st.button("🔎 Detectar redes disponíveis"):
        try:
            with st.spinner("Consultando schema..."):
                values = detect_categories()
            st.session_state.categories = values
            st.success(f"{len(values)} redes carregadas: {', '.join(values)}")
        except Exception as e:
            st.error(f"Erro: {e}")
    st.write("Lista atual:", ", ".join(st.session_state.categories))

    st.divider()
    st.subheader("Pasta de trabalho")
    st.caption(
        "A pasta não aparece na documentação pública do endpoint de metadados. "
        "O app pergunta ao schema onde esse conceito existe (filtro na listagem, campo próprio, ou "
        "campo no próprio objeto de busca) antes de aplicar qualquer filtro."
    )
    st.session_state.folder_id = st.text_input(
        "ID da pasta (ex: 25, da URL .../trac/folders/25)", value=st.session_state.folder_id
    )
    if st.button("🔎 Detectar suporte a pastas no schema"):
        try:
            with st.spinner("Consultando schema..."):
                info = detect_folder_support()
            st.session_state.folder_support = info
            found = any([info["searches_arg"], info["top_level_field"], info["search_type_folder_field"], info["create_input_field"]])
            if found:
                st.success("Suporte a pastas encontrado no schema.")
            else:
                st.warning(
                    "Não achei nada com 'folder' no nome em nenhum dos 4 lugares checados. "
                    "Pode estar com outro nome (ex: 'group', 'workspace', 'project')."
                )
            with st.expander("Detalhes da detecção"):
                st.json(info)
        except Exception as e:
            st.error(f"Erro: {e}")

    fs = st.session_state.folder_support
    if fs:
        st.caption(
            f"searches(arg)={fs['searches_arg']} · campo próprio={fs['top_level_field']} · "
            f"campo no objeto de busca={fs['search_type_folder_field']} · campo de criação={fs['create_input_field']} · "
            f"mutação de mover={fs['assign_mutation']['mutation'] if fs.get('assign_mutation') else None}"
        )

# ---- Nova busca ----
with tab_new:
    if not st.session_state.token:
        st.warning("Cole seu token na aba Configurações antes de continuar.")

    fs_preview = st.session_state.folder_support
    if fs_preview and fs_preview.get("create_input_field"):
        st.caption(f"✅ Novas buscas serão criadas direto na pasta {st.session_state.folder_id}.")
    elif fs_preview and fs_preview.get("assign_mutation"):
        st.caption(f"✅ Novas buscas são movidas para a pasta {st.session_state.folder_id} logo após criadas (via `{fs_preview['assign_mutation']['mutation']}`).")
    elif fs_preview:
        st.caption(
            "⚠️ O schema não expõe nem criação nem mutação de mover pasta — a busca nasce fora da pasta "
            "e você precisará movê-la manualmente no Pulsar Platform."
        )
    else:
        st.caption('Detecte o suporte a pastas na aba "⚙️ Configurações" para garantir que tudo caia na pasta 25.')

    name = st.text_input("Nome da busca", placeholder="ex: Cripto - Monitoramento Ago")
    auto_suffix = st.checkbox("Adicionar sufixo automático para evitar nomes duplicados", value=True)
    boolean_expr = st.text_area(
        "Expressão booleana",
        placeholder='ex: cryptocurrency AND (dump OR pump OR "to the moon")',
        height=100,
    )
    networks = st.multiselect(
        "Redes", options=st.session_state.categories,
        default=st.session_state.categories[: min(3, len(st.session_state.categories))],
        help="Se uma rede der erro de 'not available yet', ela existe no schema mas não está habilitada no seu plano.",
    )

    fb_keywords_raw = ""
    if any("facebook" in n.lower() for n in networks):
        fb_keywords_raw = st.text_area(
            "Combinações de palavras-chave do Facebook (uma combinação por linha, termos separados por vírgula)",
            placeholder="cryptocurrency, dump\ncryptocurrency, pump\ncryptocurrency, to the moon",
            height=90,
            help='Refina a busca no Facebook por pares/combinações de termos, além da booleana geral. Opcional — vira o campo "facebookKeywords" da API.',
        )

    col1, col2 = st.columns(2)
    with col1:
        start = st.date_input("Início do histórico", value=None)
    with col2:
        end = st.date_input("Fim do histórico", value=None)

    realtime = st.checkbox("Também iniciar coleta em tempo real")
    realtime_end = None
    if realtime:
        realtime_end = st.date_input(
            "Fim do tempo real (deixe em branco para rodar indefinidamente)", value=None, key="rt_end"
        )

    with st.expander("⚙️ Configurações avançadas (operadores booleanos)"):
        st.caption(
            "Essas opções viram operadores do "
            "[TRAC Full Boolean Operator Guide](https://intercom.help/pulsar/en/articles/6976653-trac-full-boolean-operator-guide) "
            "e são coladas na booleana por baixo dos panos — você não precisa escrever a sintaxe."
        )
        c1, c2, c3 = st.columns(3)
        adv_not_rt = c1.checkbox("Excluir retweets")
        adv_not_quote = c2.checkbox("Excluir citações (quotes)")
        adv_not_reply = c3.checkbox("Excluir respostas (replies)")

        adv_by_twitter = st.text_input("Somente destes perfis do X/Twitter (separados por vírgula)", placeholder="@marca, @concorrente")
        adv_not_by_twitter = st.text_input("Excluir estes perfis do X/Twitter", placeholder="@spamconta")
        adv_not_rt_of = st.text_input("Excluir retweets destes perfis", placeholder="@perfil1, @perfil2")
        adv_not_site = st.text_input("Excluir estes sites (NOT SITE)", placeholder="amazon.com, mercadolivre.com.br")
        adv_langs = st.text_input("Restringir a estes idiomas (LANG)", placeholder="pt, en")
        adv_location = st.text_input("Restringir a esta localização (LOCATION)", placeholder="BR OR US")
        adv_media = st.multiselect("Restringir a este tipo de mídia (MEDIA)", ["videos", "images"])
        adv_sample = st.number_input("Amostragem (SAMPLE, % opcional)", min_value=0, max_value=100, value=0, step=5)
        c4, c5 = st.columns(2)
        adv_pinterest = c4.checkbox("Também rodar como busca PINTEREST dedicada")
        adv_twitch = c5.checkbox("Também rodar como busca TWITCH dedicada")
        adv_extra = st.text_area(
            "Operadores extras (avançado — cole a sintaxe direto)",
            placeholder='ex: (cat OR dog) NEAR/5 (food OR toy)   |   space* AND (elon OR nasa)',
            height=70,
        )

    if st.button("🚀 Rodar busca completa", type="primary"):
        if not (name and boolean_expr and networks):
            st.error("Preencha nome, booleana e ao menos uma rede.")
        else:
            final_name = f"{name} — {datetime.now():%Y-%m-%d %H:%M:%S}" if auto_suffix else name
            fs = st.session_state.folder_support
            folder_field = fs.get("create_input_field") if fs else None
            assign_info = fs.get("assign_mutation") if fs else None

            advanced = {
                "not_rt": adv_not_rt, "not_quote": adv_not_quote, "not_reply": adv_not_reply,
                "by_twitter": adv_by_twitter, "not_by_twitter": adv_not_by_twitter, "not_rt_of": adv_not_rt_of,
                "not_site": adv_not_site, "langs": adv_langs, "location": adv_location, "media": adv_media,
                "sample": adv_sample, "pinterest": adv_pinterest, "twitch": adv_twitch, "extra": adv_extra,
            }
            final_boolean = build_boolean_expression(boolean_expr, advanced)

            progress = st.status("Rodando busca...", expanded=True)
            historic_ids = []
            search = None
            fb_keywords_parsed = []
            for line in fb_keywords_raw.splitlines():
                terms = [t.strip() for t in line.split(",") if t.strip()]
                if terms:
                    fb_keywords_parsed.append(terms)
            try:
                if folder_field:
                    progress.write(f'Criando busca ("{final_name}") na pasta {st.session_state.folder_id}...')
                else:
                    progress.write(f"Criando busca (\"{final_name}\")...")
                if final_boolean != boolean_expr.strip():
                    progress.write("Aplicando configurações avançadas na booleana...")
                if fb_keywords_parsed:
                    progress.write(f"Aplicando {len(fb_keywords_parsed)} combinação(ões) de palavras-chave do Facebook...")
                search = create_search(
                    final_name, networks, final_boolean,
                    folder_input_field=folder_field, folder_id=st.session_state.folder_id,
                    facebook_keywords=fb_keywords_parsed,
                )
                progress.write(f"✅ Busca criada — hash `{search['searchHash']}`")

                folder_assigned = bool(folder_field)
                if not folder_field and assign_info:
                    progress.write(f"Movendo para a pasta {st.session_state.folder_id}...")
                    try:
                        assign_to_folder(assign_info, search["id"], st.session_state.folder_id)
                        folder_assigned = True
                        progress.write(f"✅ Movida para a pasta {st.session_state.folder_id}")
                    except Exception as e:
                        progress.write(f"⚠️ Não consegui mover para a pasta: {e}")
                elif not folder_field and not assign_info:
                    progress.write("⚠️ Schema não suporta atribuir pasta — busca ficou fora da pasta 25.")

                historic_configured = False
                historic_ids = []
                if start and end:
                    progress.write("Configurando histórico (preview)...")
                    create_historic(search["id"], networks, iso_start(start), iso_end(end))
                    progress.write("Localizando ID do histórico...")
                    historic_ids = get_historic_ids(search["id"])
                    if not historic_ids:
                        raise RuntimeError("nenhum histórico encontrado ainda — tente novamente em instantes.")
                    historic_configured = True
                    progress.write(
                        f"✅ Histórico configurado ({len(historic_ids)} encontrado(s)) — o lançamento (o 'foguete') "
                        "acontece automaticamente assim que o preview terminar de calcular, no painel abaixo."
                    )

                if realtime:
                    progress.write("Iniciando coleta em tempo real...")
                    start_realtime(
                        search["id"],
                        iso_start(start) if start else None,
                        iso_end(realtime_end) if realtime_end else None,
                    )
                    progress.write("✅ Tempo real iniciado")

                progress.update(label="Concluído", state="complete")
                st.session_state.last_result = {
                    "name": search["name"],
                    "hash": search["searchHash"],
                    "id": search["id"],
                    "historic": historic_configured,
                    "realtime": realtime,
                    "boolean": final_boolean,
                    "folder_assigned": folder_assigned,
                }
                st.session_state.waiting_for = (
                    {"search": search, "historic_ids": historic_ids} if historic_configured else None
                )
            except Exception as e:
                progress.update(label="Erro", state="error")
                st.error(str(e))
                st.session_state.waiting_for = None

    if st.session_state.last_result:
        r = st.session_state.last_result
        st.divider()
        st.subheader("Resultado")
        st.write(f"**Busca:** {r['name']}")
        st.code(r["hash"], language=None)
        st.write(f"**Search ID:** {r['id']}")
        st.write(f"**Pasta {st.session_state.folder_id}:** {'✅ atribuída' if r.get('folder_assigned') else '⚠️ não atribuída — mova manualmente'}")
        if not r.get("folder_assigned"):
            fs_now = st.session_state.folder_support
            if not fs_now:
                st.caption(
                    'Isso costuma ser porque a detecção de pastas ainda não rodou. Vá em '
                    '"⚙️ Configurações" → "Detectar suporte a pastas no schema" antes da próxima busca.'
                )
            else:
                st.caption("O schema não expôs nem criação nem edição com campo de pasta — confirmado via introspection, não é falta de tentativa.")
        st.write(f"**Histórico:** {'configurado — acompanhe o lançamento automático abaixo' if r['historic'] else 'não solicitado'}")
        st.write(f"**Tempo real:** {'iniciado' if r['realtime'] else 'não solicitado'}")
        if r.get("boolean"):
            with st.expander("Booleana final enviada (com configurações avançadas aplicadas)"):
                st.code(r["boolean"], language=None)

        track = st.session_state.get("waiting_for")
        if track:
            st.divider()
            st.subheader("Progresso completo — preview, lançamento e coleta")
            search_obj = track["search"]
            historic_ids = track["historic_ids"]
            st.session_state.waiting_for = None  # this single call handles the whole flow end-to-end

            placeholder = st.empty()
            outcome, payload, extra = run_full_historic_flow(placeholder, search_obj, historic_ids)

            if outcome == "done":
                df, used_field = payload, extra
                st.session_state.results_df = df
                st.session_state.results_field_used = used_field
                st.success(f"{len(df)} registro(s) carregado(s) via `{used_field}`.")
                st.divider()
                render_dashboard(df, key_prefix="auto_")
            elif outcome == "failed":
                st.error(f"O preview falhou no Pulsar (status: {payload}). Confira no Pulsar Platform.")
            elif outcome == "launch_failed":
                st.error(f"O preview terminou mas o lançamento (foguete) falhou: {payload}")
            else:  # long_wait
                attempts, last_status_text = payload, extra
                st.info(
                    f"Ainda coletando depois de bastante tempo (último status: {last_status_text}). "
                    'Isso pode ser normal para períodos grandes. Você pode ir na aba "📊 Resultados" a qualquer '
                    'momento, escolher esse mesmo estudo e clicar em "Puxar TUDO desse estudo" — funciona assim '
                    "que os dados estiverem prontos, sem precisar recriar nada."
                )
                if attempts:
                    render_fetch_attempts(attempts)
        elif r.get("historic"):
            st.caption('Vá até a aba "📊 Resultados" para puxar o feed e montar o dashboard.')

# ---- Minhas buscas ----
with tab_list:
    if not st.session_state.token:
        st.warning("Cole seu token na aba Configurações antes de continuar.")
    else:
        fs = st.session_state.folder_support
        restrict = st.checkbox(f"Mostrar só a pasta {st.session_state.folder_id}", value=True)

        if st.button("🔄 Atualizar lista"):
            try:
                if restrict and fs and fs.get("top_level_field"):
                    with st.spinner(f"Buscando pasta {st.session_state.folder_id} via {fs['top_level_field']}..."):
                        st.session_state.searches = fetch_folder_searches(fs["top_level_field"], st.session_state.folder_id)
                elif restrict and fs and (fs.get("searches_arg") or fs.get("search_type_folder_field")):
                    st.session_state.searches = list_searches(folder_support=fs, folder_id=st.session_state.folder_id)
                else:
                    if restrict:
                        st.info(
                            'Ainda não detectei suporte a pastas — mostrando todas as buscas. '
                            'Rode "Detectar suporte a pastas" na aba Configurações.'
                        )
                    st.session_state.searches = list_searches()
            except Exception as e:
                st.error(str(e))

        if not st.session_state.searches:
            st.info('Clique em "Atualizar lista" para carregar suas buscas.')
        else:
            header = st.columns([3, 2, 2, 2, 2])
            for col, label in zip(header, ["Nome", "Status", "Tempo real", "Conteúdos", ""]):
                col.markdown(f"**{label}**")
            for s in st.session_state.searches:
                cols = st.columns([3, 2, 2, 2, 2])
                cols[0].write(s["name"])
                cols[1].write(s.get("status") or "-")
                is_on = s.get("realtimeStatus") == "STARTED"
                cols[2].write(("🟢 " if is_on else "⚪ ") + str(s.get("realtimeStatus") or "-"))
                cols[3].write(str(s.get("totalContents", "-")))
                if is_on:
                    if cols[4].button("Parar", key=f"stop-{s['id']}"):
                        try:
                            stop_search(s["id"])
                            st.success("Parado.")
                            if restrict and fs and fs.get("top_level_field"):
                                st.session_state.searches = fetch_folder_searches(fs["top_level_field"], st.session_state.folder_id)
                            elif restrict and fs and (fs.get("searches_arg") or fs.get("search_type_folder_field")):
                                st.session_state.searches = list_searches(folder_support=fs, folder_id=st.session_state.folder_id)
                            else:
                                st.session_state.searches = list_searches()
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

# ---- Resultados / Dashboard ----
with tab_results:
    if not st.session_state.token:
        st.warning("Cole seu token na aba Configurações antes de continuar.")
    else:
        st.subheader("1. Escolha o estudo")
        if not st.session_state.searches:
            fs = st.session_state.folder_support
            try:
                if fs and (fs.get("searches_arg") or fs.get("search_type_folder_field")):
                    st.session_state.searches = list_searches(folder_support=fs, folder_id=st.session_state.folder_id)
                elif fs and fs.get("top_level_field"):
                    st.session_state.searches = fetch_folder_searches(fs["top_level_field"], st.session_state.folder_id)
                else:
                    st.session_state.searches = list_searches()
            except Exception as e:
                st.error(str(e))

        # belt-and-suspenders: whatever the source, patch any missing names before showing the picker
        st.session_state.searches = enrich_names(st.session_state.searches)

        def _label(s):
            nm = s.get("name") or f"(sem nome) {s.get('searchHash', s.get('id', '?'))}"
            return f"{nm} — {s.get('searchHash', '')}"

        search_options = {_label(s): s for s in st.session_state.searches}
        if st.button("🔄 Recarregar lista de estudos"):
            st.session_state.searches = []
            st.rerun()

        picked_label = st.selectbox("Estudo", list(search_options.keys())) if search_options else None
        picked = search_options.get(picked_label) if picked_label else None

        if picked and st.button("📥 Puxar TUDO desse estudo", type="primary"):
            try:
                with st.spinner("Descobrindo o melhor campo e buscando todos os campos disponíveis..."):
                    df, used_field, attempts = fetch_all_results_auto(picked)
                if df.empty:
                    st.error("Não consegui trazer resultados automaticamente.")
                    render_fetch_attempts(attempts)
                else:
                    st.session_state.results_df = df
                    st.session_state.results_field_used = used_field
                    st.success(f"{len(df)} registro(s) carregado(s) via `{used_field}`, com {len(df.columns)} coluna(s).")
            except Exception as e:
                st.error(str(e))


        df = st.session_state.get("results_df")
        if df is not None and not df.empty:
            st.divider()
            st.subheader("2. Planilha e dashboard")
            render_dashboard(df, key_prefix="res_")
        elif df is not None:
            st.info("A busca retornou 0 registros para esse período/rede.")
