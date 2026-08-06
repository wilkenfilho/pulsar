import streamlit as st
import requests

ENDPOINT = "https://trac.pulsarplatform.com/graphql"
DEFAULT_CATEGORIES = ["REDDIT", "FORUMS", "FACEBOOK"]

st.set_page_config(page_title="Radar Simples — Pulsar", page_icon="📡", layout="centered")

# ---------------- session defaults ----------------
if "token" not in st.session_state:
    st.session_state.token = ""
if "categories" not in st.session_state:
    st.session_state.categories = DEFAULT_CATEGORIES
if "last_result" not in st.session_state:
    st.session_state.last_result = None
if "searches" not in st.session_state:
    st.session_state.searches = []


# ---------------- GraphQL helpers ----------------
def gql(query, variables=None):
    token = st.session_state.token
    if not token:
        raise RuntimeError("Cole seu token na aba Configurações antes de continuar.")
    try:
        resp = requests.post(
            ENDPOINT,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Falha de rede ao chamar a Pulsar: {e}")

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
    data = gql(type_query)
    input_fields = (data.get("__type") or {}).get("inputFields") or []
    cat_field = next((f for f in input_fields if "categor" in f["name"].lower()), None)
    if not cat_field:
        raise RuntimeError('campo de categorias não encontrado no schema (introspection pode estar desabilitada).')
    named = unwrap_named_type(cat_field["type"])
    if not named or not named.get("name"):
        raise RuntimeError(f'não consegui resolver o tipo enum de "{cat_field["name"]}".')
    enum_query = f'query {{ __type(name: "{named["name"]}") {{ enumValues {{ name }} }} }}'
    enum_data = gql(enum_query)
    values = [v["name"] for v in (enum_data.get("__type") or {}).get("enumValues") or []]
    if not values:
        raise RuntimeError(f'enum "{named["name"]}" retornou vazio.')
    return values


def create_search(name, categories, boolean_expr):
    mutation = """
    mutation CreateBoolTopic($input: CreateBooleanTopicsSearchInput!){
      createBooleanTopicsSearch(input:$input){
        search{ id name searchHash }
        errors{ id message extensions }
      }
    }"""
    data = gql(mutation, {"input": {"name": name, "categories": categories, "booleanExpression": boolean_expr}})
    result = data["createBooleanTopicsSearch"]
    if result.get("errors"):
        raise RuntimeError(" | ".join(e["message"] for e in result["errors"]))
    return result["search"]


def create_historic(search_id, categories, start_iso, end_iso):
    mutation = """
    mutation CreateHistoric($input: CreateHistoricInput!){
      createHistoric(input:$input){ errors{ id message extensions } }
    }"""
    data = gql(mutation, {"input": {"searchId": search_id, "categories": categories,
                                     "startDate": start_iso, "endDate": end_iso}})
    errors = data["createHistoric"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(e["message"] for e in errors))


def get_historic_ids(search_id):
    query = """
    query PreviewHistorics($sid: ID){
      historics(searchId:$sid){ nodes{ id status } }
    }"""
    data = gql(query, {"sid": search_id})
    return [n["id"] for n in data["historics"]["nodes"]]


def launch_historic(ids):
    mutation = """
    mutation launchHistoric($input: LaunchHistoricInput!){
      launchHistoric(input:$input){ errors{ id message extensions } }
    }"""
    data = gql(mutation, {"input": {"ids": ids}})
    errors = data["launchHistoric"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(e["message"] for e in errors))


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
    data = gql(mutation, {"input": inp})
    errors = data["startSearch"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(e["message"] for e in errors))


def stop_search(search_id):
    mutation = """
    mutation stopSearch($input: StopSearchInput!){
      stopSearch(input:$input){ errors{ id message extensions } }
    }"""
    data = gql(mutation, {"input": {"id": search_id}})
    errors = data["stopSearch"].get("errors")
    if errors:
        raise RuntimeError(" | ".join(e["message"] for e in errors))


def list_searches():
    query = """
    query Searches($sortBy: AllowedFieldsForSorting){
      searches(sortBy:$sortBy){
        totalCount
        nodes{ id name totalContents startDate status searchHash realtimeStatus }
      }
    }"""
    data = gql(query, {"sortBy": "NAME"})
    return data["searches"]["nodes"]


def iso_start(d):
    return f"{d.isoformat()}T00:00:00Z" if d else None


def iso_end(d):
    return f"{d.isoformat()}T23:59:59Z" if d else None


# ---------------- UI ----------------
st.title("📡 Radar Simples — Pulsar")
st.caption("Boolean + redes + período → busca criada e histórico lançado, sem passar pelas 9 telas do Pulsar.")

tab_config, tab_new, tab_list = st.tabs(["⚙️ Configurações", "🔍 Nova busca", "📋 Minhas buscas"])

# ---- Configurações ----
with tab_config:
    st.subheader("Token de autorização")
    st.session_state.token = st.text_input(
        "Bearer Token",
        value=st.session_state.token,
        type="password",
        help="Emitido pelo seu Account Manager do Pulsar. Fica só na sessão deste app, em memória — não é salvo em disco nem em cookies.",
    )
    st.divider()
    st.subheader("Redes disponíveis")
    st.caption(
        "A documentação pública só confirma REDDIT, FORUMS e FACEBOOK como exemplos de valores. "
        "Use o botão abaixo para consultar o schema real do seu ambiente via introspection GraphQL."
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
    boolean_expr = st.text_area(
        "Expressão booleana",
        placeholder='ex: cryptocurrency AND (dump OR pump OR "to the moon")',
        height=100,
    )
    networks = st.multiselect(
        "Redes", options=st.session_state.categories,
        default=st.session_state.categories[: min(3, len(st.session_state.categories))],
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
            progress = st.status("Rodando busca...", expanded=True)
            try:
                progress.write("Criando busca...")
                search = create_search(name, networks, boolean_expr)
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
        st.caption("Abra a busca no Pulsar Platform pelo hash acima para acompanhar o feed e exportar quando quiser.")

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
