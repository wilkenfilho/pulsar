import io
import re
from collections import Counter
from datetime import datetime
from itertools import combinations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

TRAC_ENDPOINT = "https://trac.pulsarplatform.com/graphql"
DATA_ENDPOINT = "https://data.pulsarplatform.com/graphql/trac"
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


def data_gql(query, variables=None):
    return gql(DATA_ENDPOINT, query, variables)


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


def detect_categories():
    type_query = """
    query {
      __type(name: "CreateBooleanTopicsSearchInput") {
        inputFields {
          name
          type {
            kind name
            ofType { kind name
              ofType { kind name
                ofType { kind name
                  ofType { kind name }
                }
              }
            }
          }
        }
      }
    }"""
    data = trac_gql(type_query)
    input_fields = (data.get("__type") or {}).get("inputFields") or []
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
    return values


# ---------------- TRAC metadata mutations ----------------
def create_search(name, categories, boolean_expr):
    mutation = """
    mutation CreateBoolTopic($input: CreateBooleanTopicsSearchInput!){
      createBooleanTopicsSearch(input:$input){
        search{ id name searchHash }
        errors{ id message extensions }
      }
    }"""
    data = trac_gql(mutation, {"input": {"name": name, "categories": categories, "booleanExpression": boolean_expr}})
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


def list_searches():
    query = """
    query Searches($sortBy: AllowedFieldsForSorting){
      searches(sortBy:$sortBy){
        totalCount
        nodes{ id name totalContents startDate status searchHash realtimeStatus }
      }
    }"""
    data = trac_gql(query, {"sortBy": "NAME"})
    return data["searches"]["nodes"]


def iso_start(d):
    return f"{d.isoformat()}T00:00:00Z" if d else None


def iso_end(d):
    return f"{d.isoformat()}T23:59:59Z" if d else None


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


def find_results_field_candidates(fields):
    keywords = ("content", "post", "result", "trac", "record", "item")
    return [f for f in fields if any(k in f["name"].lower() for k in keywords)]


def build_dynamic_query(endpoint, field_name, arg_values):
    field_info = next(f for f in introspect_query_fields(endpoint) if f["name"] == field_name)
    return_named = unwrap_named_type(field_info["type"])
    result_type = introspect_type_fields(endpoint, return_named["name"])

    nodes_field = next((f for f in (result_type.get("fields") or []) if f["name"] in ("nodes", "edges", "items", "results")), None)

    if nodes_field:
        node_named = unwrap_named_type(nodes_field["type"])
        if node_named["name"] and (result_type.get("kind") != "SCALAR"):
            # if edges (relay style), go one level deeper to "node"
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
        leaf_fields = leaf_fields[:60] or ["id"]
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
        # field returns a flat list directly
        leaf_fields = [f["name"] for f in (result_type.get("fields") or []) if unwrap_named_type(f["type"]).get("kind") in LEAF_KINDS]
        leaf_fields = leaf_fields[:60] or ["id"]
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

# ---- Nova busca ----
with tab_new:
    if not st.session_state.token:
        st.warning("Cole seu token na aba Configurações antes de continuar.")

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

    if st.button("🚀 Rodar busca completa", type="primary"):
        if not (name and boolean_expr and networks):
            st.error("Preencha nome, booleana e ao menos uma rede.")
        else:
            final_name = f"{name} — {datetime.now():%Y-%m-%d %H:%M:%S}" if auto_suffix else name
            progress = st.status("Rodando busca...", expanded=True)
            try:
                progress.write(f"Criando busca (\"{final_name}\")...")
                search = create_search(final_name, networks, boolean_expr)
                progress.write(f"✅ Busca criada — hash `{search['searchHash']}`")

                historic_launched = False
                if start and end:
                    progress.write("Configurando histórico...")
                    create_historic(search["id"], networks, iso_start(start), iso_end(end))
                    progress.write("Localizando ID do histórico...")
                    ids = get_historic_ids(search["id"])
                    if not ids:
                        raise RuntimeError("nenhum histórico encontrado ainda — tente novamente em instantes.")
                    progress.write(f"Lançando histórico ({len(ids)} encontrado(s))...")
                    launch_historic(ids)
                    historic_launched = True
                    progress.write("✅ Histórico lançado")

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
                    "historic": historic_launched,
                    "realtime": realtime,
                }
            except Exception as e:
                progress.update(label="Erro", state="error")
                st.error(str(e))

    if st.session_state.last_result:
        r = st.session_state.last_result
        st.divider()
        st.subheader("Resultado")
        st.write(f"**Busca:** {r['name']}")
        st.code(r["hash"], language=None)
        st.write(f"**Search ID:** {r['id']}")
        st.write(f"**Histórico lançado:** {'sim' if r['historic'] else 'não solicitado'}")
        st.write(f"**Tempo real:** {'iniciado' if r['realtime'] else 'não solicitado'}")
        st.caption('Vá até a aba "📊 Resultados" para puxar o feed e montar o dashboard.')

# ---- Minhas buscas ----
with tab_list:
    if not st.session_state.token:
        st.warning("Cole seu token na aba Configurações antes de continuar.")
    else:
        if st.button("🔄 Atualizar lista"):
            try:
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
                            st.session_state.searches = list_searches()
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

# ---- Resultados / Dashboard ----
with tab_results:
    if not st.session_state.token:
        st.warning("Cole seu token na aba Configurações antes de continuar.")
    else:
        st.subheader("1. Descobrir o campo de resultados")
        st.caption(
            "O Data Endpoint (data.pulsarplatform.com/graphql/trac) não tem schema público documentado, "
            "então o app pergunta ao próprio GraphQL quais campos existem — em vez de eu chutar nomes."
        )
        if st.button("🔬 Descobrir campos no Data Endpoint"):
            try:
                with st.spinner("Consultando schema do Data Endpoint..."):
                    fields = introspect_query_fields(DATA_ENDPOINT)
                candidates = find_results_field_candidates(fields)
                st.session_state.field_candidates = candidates or fields
                st.success(f"{len(st.session_state.field_candidates)} campo(s) candidato(s) encontrado(s).")
            except Exception as e:
                st.error(f"Erro na introspection: {e}")

        candidates = st.session_state.get("field_candidates", [])
        if candidates:
            names = [f["name"] for f in candidates]
            chosen = st.selectbox("Campo de resultados a usar", names)
            field_info = next(f for f in candidates if f["name"] == chosen)
            arg_names = [a["name"] for a in field_info["args"]]
            st.caption(f"Argumentos deste campo: {', '.join(arg_names) if arg_names else '(nenhum)'}")

            st.subheader("2. Selecionar busca e período")
            if not st.session_state.searches:
                try:
                    st.session_state.searches = list_searches()
                except Exception as e:
                    st.error(str(e))
            search_options = {f"{s['name']} — {s['searchHash']}": s for s in st.session_state.searches}
            picked_label = st.selectbox("Busca", list(search_options.keys())) if search_options else None
            picked = search_options.get(picked_label) if picked_label else None

            arg_values = {}
            for a in field_info["args"]:
                gql_type_ref = a["type"]
                named = unwrap_named_type(gql_type_ref)
                type_str = named.get("name") or "String"
                is_id_like = any(k in a["name"].lower() for k in ("id", "hash", "search"))
                if is_id_like and picked:
                    default_val = picked["id"] if "id" in a["name"].lower() else picked["searchHash"]
                    val = st.text_input(f"Arg: {a['name']} ({type_str})", value=str(default_val), key=f"arg-{a['name']}")
                else:
                    val = st.text_input(f"Arg: {a['name']} ({type_str})", value="", key=f"arg-{a['name']}")
                if val:
                    arg_values[a["name"]] = {"value": val, "gql_type": type_str if gql_type_ref.get("kind") == "NON_NULL" else type_str}

            if st.button("📥 Buscar resultados", type="primary"):
                try:
                    with st.spinner("Montando query dinâmica e buscando..."):
                        query, nodes_key = build_dynamic_query(DATA_ENDPOINT, chosen, arg_values)
                        variables = {k: v["value"] for k, v in arg_values.items()}
                        payload = data_gql(query, variables)
                        df = flatten_results(payload, chosen, nodes_key)
                    st.session_state.results_df = df
                    st.session_state.results_query = query
                    st.success(f"{len(df)} registro(s) carregado(s).")
                except Exception as e:
                    st.error(str(e))
                    if st.session_state.get("results_query") is None:
                        st.caption("Se o erro for de campo desconhecido, tente outro campo candidato acima.")

        df = st.session_state.get("results_df")
        if df is not None and not df.empty:
            st.divider()
            st.subheader("3. Planilha")
            st.dataframe(df, use_container_width=True)
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="resultados")
            st.download_button("⬇️ Baixar como Excel", buf.getvalue(), file_name="pulsar_resultados.xlsx")

            st.divider()
            st.subheader("4. Mapear colunas para os gráficos")
            cols = list(df.columns)

            def guess(keywords, default_idx=0):
                for kw in keywords:
                    for c in cols:
                        if kw in c.lower():
                            return c
                return cols[default_idx] if cols else None

            c1, c2, c3, c4 = st.columns(4)
            text_col = c1.selectbox("Coluna de texto", cols, index=cols.index(guess(["text", "content", "body", "message"])) if guess(["text", "content", "body", "message"]) in cols else 0)
            date_col = c2.selectbox("Coluna de data", cols, index=cols.index(guess(["date", "created", "time", "publish"])) if guess(["date", "created", "time", "publish"]) in cols else 0)
            sentiment_col = c3.selectbox("Coluna de sentimento (opcional)", ["(nenhuma)"] + cols)
            network_col = c4.selectbox("Coluna de rede (opcional)", ["(nenhuma)"] + cols)

            st.divider()
            st.subheader("5. Dashboard")

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
        elif df is not None:
            st.info("A busca retornou 0 registros para esse período/rede.")
