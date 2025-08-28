# app.py
# -*- coding: utf-8 -*-

import os
from typing import Optional, Dict, Any, Tuple

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# --- import seguro do dotenv (tolerante se o pacote não estiver instalado) ---
try:
    from dotenv import load_dotenv
except Exception:
    def load_dotenv(*args, **kwargs):
        return False

# ==============================
# Configuração de página e estilo
# ==============================
st.set_page_config(
    page_title="CorreigeAI • Conferência de Redações",
    page_icon="📝",
    layout="wide",
    initial_sidebar_state="expanded",
)

CUSTOM_CSS = """
<style>
/* Layout */
.block-container { padding-top: 1rem; padding-bottom: 1rem; }
header[data-testid="stHeader"] { backdrop-filter: blur(6px); }

/* Badges de status */
.badge {
  display:inline-block; padding: 0.25rem 0.7rem; border-radius: 999px;
  font-size: 0.8rem; font-weight: 700; border: 1px solid transparent;
}
.badge.status-ok   { background: #DCFCE7; color:#14532D; border-color:#86EFAC; }   /* Atualizado */
.badge.status-warn { background: #FEE2E2; color:#7F1D1D; border-color:#FCA5A5; }   /* Não atualizado */

/* Cards de resumo */
.stat-card {
  border:1px solid #E2E8F0; border-radius: 14px; padding: 1rem; background:white;
  box-shadow: 0 1px 3px rgba(0,0,0,0.03);
}
.stat-value { font-size: 1.2rem; font-weight: 700; }
.stat-label { font-size: 0.85rem; color: #475569; }

/* Barra superior */
.toolbar {
  border:1px solid #E2E8F0; border-radius: 14px; padding: 0.75rem; background:white;
  display:flex; gap:0.5rem; align-items:center; justify-content:space-between;
}

/* Editor */
textarea {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
}

/* Componente de zoom por clique */
.cz-wrap {
  position: relative;
  width: 100%;
  overflow: hidden;
  border-radius: 12px;
  border: 1px solid #E2E8F0;
  background: #fff;
  user-select: none;
}
.cz-img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;              /* imagem completa no estado inicial */
  transform-origin: var(--cx, 50%) var(--cy, 50%);
  transition: transform 120ms ease, translate 120ms ease, cursor 120ms ease;
  cursor: zoom-in;
}
.cz-hint {
  position: absolute;
  right: 10px; bottom: 10px;
  background: rgba(15,23,42,.75);
  color: #fff;
  padding: 6px 10px;
  border-radius: 999px;
  font-size: 12px;
  z-index: 3;
}
.cz-controls {
  position: absolute;
  left: 10px; bottom: 10px;
  display: flex; gap: 6px;
  z-index: 4;
}
.cz-btn {
  background: rgba(255,255,255,.95);
  border: 1px solid #CBD5E1;
  border-radius: 8px;
  padding: 4px 10px;
  font-weight: 700;
  font-size: 14px;
  box-shadow: 0 1px 2px rgba(0,0,0,.06);
  cursor: pointer;
}
.cz-btn:active { transform: translateY(1px); }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ==============================
# Utilidades
# ==============================
def _clean_env(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1].strip()
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    return s

def _safe_image_url(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    # Se preferir, defina um prefixo do seu bucket:
    # base = "https://seu-bucket.s3.sa-east-1.amazonaws.com/"
    # return base + raw.lstrip("/")
    return raw

# ==============================
# Engine: st.secrets → .env → os.environ
# ==============================
@st.cache_resource(show_spinner=False)
def build_engine() -> Engine:
    # 1) Secrets (ideal p/ Streamlit Cloud)
    secrets_db = {}
    try:
        secrets_db = st.secrets.get("db", st.secrets)
    except Exception:
        secrets_db = {}

    def _get(name: str, default: str = "") -> str:
        if name in secrets_db:
            return str(secrets_db.get(name, default))
        return os.getenv(name, default)

    # 2) .env (se disponível)
    load_dotenv()

    conn = _get("DB_CONNECTION", "mysql").lower()
    if conn != "mysql":
        raise RuntimeError("Somente MySQL é suportado (DB_CONNECTION=mysql).")

    host = _clean_env(_get("DB_HOST", ""))
    port = int(_get("DB_PORT", "3306") or "3306")
    db   = _clean_env(_get("DB_DATABASE", "corrigeai"))
    user = _clean_env(_get("DB_USERNAME", "udb"))
    pw   = _clean_env(_get("DB_PASSWORD", ""))

    if not host or not pw:
        raise RuntimeError(
            "Credenciais do banco ausentes. Defina em st.secrets ou no .env: "
            "DB_HOST, DB_DATABASE, DB_USERNAME, DB_PASSWORD."
        )

    url = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{db}?charset=utf8mb4"
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)

# ==============================
# Zoom por clique (com wheel) + anti-click pós-pan
# ==============================
def render_click_zoom(image_url: str, height_px: int = 760, step: float = 0.5, max_scale: float = 4.0, min_scale: float = 1.0):
    """
    - Clique normal: aumenta zoom.
    - Shift + clique: diminui zoom.
    - Roda do mouse: aumenta/diminui zoom (para frente + / para trás -).
    - Duplo clique: reset.
    - Arraste (mouse/touch): move quando ampliada.
    - Anti-zoom após pan: ignora o 'click' que vem logo depois de arrastar.
    - Mantém o comportamento visual original (imagem aparece inteira).
    """
    from streamlit.components.v1 import html

    html(f"""
    <style>
      .cz-wrap {{
        position: relative;
        width: 100%;
        height: {height_px}px;            /* altura visível do contêiner */
        overflow: hidden;
        border-radius: 12px;
        border: 1px solid #E2E8F0;
        background: #fff;
        user-select: none;
      }}
      .cz-img {{
        display: block;
        width: 100%;
        height: 100%;
        object-fit: contain;              /* garante imagem inteira no estado inicial */
        transform-origin: var(--cx, 50%) var(--cy, 50%);
        transition: transform 120ms ease, translate 120ms ease, cursor 120ms ease;
        cursor: zoom-in;
      }}
    </style>

    <div class="cz-wrap" id="cz-wrap">
      <img id="cz-img" class="cz-img" src="{image_url}" alt="Redação" />
    </div>

    <script>
    (function(){{
      const img = document.getElementById('cz-img');
      const wrap = document.getElementById('cz-wrap');

      let scale = {min_scale};           // 1.0 (tamanho normal)
      const step = {step};
      const maxScale = {max_scale};
      const minScale = {min_scale};

      let posX = 0, posY = 0;            // translate em px
      let isPanning = false;
      let startX = 0, startY = 0;

      // ---- flags anti-click pós-pan ----
      const DRAG_THRESHOLD = 5;          // px de deslocamento para considerar "pan"
      let downX = 0, downY = 0;
      let movedSinceDown = false;
      let ignoreNextClick = false;

      function clamp(v, lo, hi) {{
        return Math.max(lo, Math.min(hi, v));
      }}

      function applyTransform() {{
        img.style.transform = 'scale(' + scale + ') translate(' + posX + 'px, ' + posY + 'px)';
        img.style.cursor = scale > 1 ? (isPanning ? 'grabbing' : 'grab') : 'zoom-in';
      }}

      function setOriginFromEvent(e) {{
        const rect = img.getBoundingClientRect();
        const clientX = e.clientX || (e.touches && e.touches[0].clientX);
        const clientY = e.clientY || (e.touches && e.touches[0].clientY);
        if (clientX == null || clientY == null) return;
        const x = (clientX - rect.left) / rect.width * 100;
        const y = (clientY - rect.top)  / rect.height * 100;
        img.style.setProperty('--cx', x + '%');
        img.style.setProperty('--cy', y + '%');
      }}

      function zoomBy(delta, e) {{
        const prev = scale;
        scale = clamp(parseFloat((scale + delta).toFixed(2)), minScale, maxScale);
        if (prev !== scale && e) setOriginFromEvent(e);
        if (scale === minScale) {{ posX = 0; posY = 0; }}
        applyTransform();
      }}

      // Clique: +zoom | Shift+clique: -zoom (com antirruído pós-pan)
      wrap.addEventListener('click', (e) => {{
        if (ignoreNextClick) {{ ignoreNextClick = false; return; }}  // suprime click após pan
        if (e.detail > 1) return;  // evita duplicar com dblclick
        if (typeof e.button === 'number' && e.button !== 0) return; // só botão esquerdo
        zoomBy(e.shiftKey ? -step : step, e);
      }});

      // Roda do mouse: ± zoom (para frente +, para trás -)
      wrap.addEventListener('wheel', (e) => {{
        e.preventDefault();
        const delta = e.deltaY < 0 ? step : -step;
        zoomBy(delta, e);
      }}, {{passive:false}});

      // Duplo clique: reset
      wrap.addEventListener('dblclick', () => {{
        scale = minScale; posX = 0; posY = 0; applyTransform();
      }});

      // Pan com mouse
      wrap.addEventListener('mousedown', (e) => {{
        if (typeof e.button === 'number' && e.button !== 0) return; // só botão esquerdo
        if (scale <= minScale) return;    // só pan quando ampliada
        isPanning = true;
        movedSinceDown = false;
        downX = e.clientX; downY = e.clientY;
        startX = e.clientX - posX;
        startY = e.clientY - posY;
        img.style.cursor = 'grabbing';
        e.preventDefault();
      }});

      window.addEventListener('mousemove', (e) => {{
        if (!isPanning) return;
        posX = e.clientX - startX;
        posY = e.clientY - startY;

        // marca como "moveu" se passou do limiar
        if (!movedSinceDown) {{
          if (Math.abs(e.clientX - downX) > DRAG_THRESHOLD || Math.abs(e.clientY - downY) > DRAG_THRESHOLD) {{
            movedSinceDown = true;
          }}
        }}
        applyTransform();
      }});

      window.addEventListener('mouseup', () => {{
        if (!isPanning) return;
        isPanning = false;
        if (movedSinceDown) {{
          // Se houve pan real, ignore o click que o navegador dispara depois do mouseup
          ignoreNextClick = true;
          setTimeout(() => {{ ignoreNextClick = false; }}, 250);
        }}
        if (scale > minScale) img.style.cursor = 'grab';
      }});

      // Touch (mobile) – arrastar
      wrap.addEventListener('touchstart', (e) => {{
        if (scale <= minScale) return;
        const t = e.touches[0];
        isPanning = true;
        movedSinceDown = false;
        downX = t.clientX; downY = t.clientY;
        startX = t.clientX - posX;
        startY = t.clientY - posY;
      }}, {{passive:true}});

      wrap.addEventListener('touchmove', (e) => {{
        if (!isPanning) return;
        const t = e.touches[0];
        posX = t.clientX - startX;
        posY = t.clientY - startY;

        if (!movedSinceDown) {{
          if (Math.abs(t.clientX - downX) > DRAG_THRESHOLD || Math.abs(t.clientY - downY) > DRAG_THRESHOLD) {{
            movedSinceDown = true;
          }}
        }}
        applyTransform();
      }}, {{passive:true}});

      wrap.addEventListener('touchend', () => {{
        if (!isPanning) return;
        isPanning = false;
        if (movedSinceDown) {{
          ignoreNextClick = true;
          setTimeout(() => {{ ignoreNextClick = false; }}, 250);
        }}
      }});

      // Inicial
      applyTransform();
    }})();
    </script>
    """, height=height_px+6, scrolling=False)

# ==============================
# Acesso ao banco (tudo em textos_digitados)
# ==============================
def get_resumo(engine: Engine) -> Tuple[int, int, int]:
    q = text("""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN COALESCE(status,0)=0 THEN 1 ELSE 0 END) AS pendentes,
          SUM(CASE WHEN COALESCE(status,0)=1 THEN 1 ELSE 0 END) AS concluidos
        FROM textos_digitados
    """)
    with engine.connect() as c:
        r = c.execute(q).mappings().first()
        total = int(r["total"] or 0)
        pend = int(r["pendentes"] or 0)
        conc = int(r["concluidos"] or 0)
        return total, pend, conc

def listar_redacoes(engine: Engine, somente_pendentes: bool, busca: str) -> pd.DataFrame:
    base_sql = """
        SELECT
          redacao_id,
          arquivo_nome_armazenamento AS imagem_url,
          COALESCE(status,0) AS status,
          COALESCE(texto_digitado,'') AS texto_digitado
        FROM textos_digitados
    """
    where = []
    params = {}
    if somente_pendentes:
        where.append("COALESCE(status,0) = 0")
    if busca:
        where.append("CAST(redacao_id AS CHAR) LIKE :busca")
        params["busca"] = f"%{busca}%"

    if where:
        base_sql += " WHERE " + " AND ".join(where)
    base_sql += " ORDER BY redacao_id ASC"

    with engine.connect() as c:
        df = pd.read_sql(text(base_sql), c, params=params)
    df["imagem_url"] = df["imagem_url"].apply(_safe_image_url)
    return df

def carregar_redacao(engine: Engine, redacao_id: int) -> Dict[str, Any]:
    q = text("""
        SELECT
          redacao_id,
          arquivo_nome_armazenamento AS imagem_url,
          COALESCE(status,0) AS status,
          COALESCE(texto_digitado,'') AS texto_digitado
        FROM textos_digitados
        WHERE redacao_id = :rid
        LIMIT 1
    """)
    with engine.connect() as c:
        r = c.execute(q, {"rid": redacao_id}).mappings().first()
        if not r:
            raise ValueError(f"redacao_id {redacao_id} não encontrado em textos_digitados.")
        return {
            "redacao_id": int(r["redacao_id"]),
            "imagem_url": _safe_image_url(r["imagem_url"]),
            "status": int(r["status"] or 0),
            "texto_digitado": r["texto_digitado"] or "",
        }

def salvar_texto(engine: Engine, redacao_id: int, novo_texto: str, imagem_url: Optional[str]) -> None:
    """
    Atualiza textos_digitados (status=1). Se não existir, insere com a imagem informada.
    """
    upd = text("""
        UPDATE textos_digitados
           SET texto_digitado = :txt, status = 1
         WHERE redacao_id = :rid
    """)
    ins = text("""
        INSERT INTO textos_digitados (redacao_id, texto_digitado, status, arquivo_nome_armazenamento)
        VALUES (:rid, :txt, 1, :img)
    """)
    with engine.begin() as c:
        res = c.execute(upd, {"txt": novo_texto, "rid": redacao_id})
        if res.rowcount == 0:
            c.execute(ins, {"rid": redacao_id, "txt": novo_texto, "img": imagem_url or ""})

# ==============================
# Estado de sessão
# ==============================
if "selecionado" not in st.session_state:
    st.session_state.selecionado = None
if "loaded_redacao_id" not in st.session_state:
    st.session_state.loaded_redacao_id = None
if "last_saved_text" not in st.session_state:
    st.session_state.last_saved_text = ""
if "texto_digitado_input" not in st.session_state:
    st.session_state.texto_digitado_input = ""

# ==============================
# Inicialização / conexão
# ==============================
try:
    engine = build_engine()
except Exception as e:
    st.error("Falha ao conectar no banco de dados. Defina credenciais em st.secrets ou .env.")
    st.exception(e)
    st.stop()

try:
    total, pendentes, concluidos = get_resumo(engine)
except SQLAlchemyError as e:
    st.error("Erro ao consultar o resumo.")
    st.exception(e)
    st.stop()

# ==============================
# Resumo – cards
# ==============================
col_a, col_b, col_c = st.columns(3)
with col_a:
    st.markdown(f'</br><div class="stat-card"><div class="stat-value">{total}</div><div class="stat-label">Total</div></div>', unsafe_allow_html=True)
with col_b:
    st.markdown(f'</br><div class="stat-card"><div class="stat-value">{pendentes}</div><div class="stat-label">Não atualizadas</div></div>', unsafe_allow_html=True)
with col_c:
    st.markdown(f'</br><div class="stat-card"><div class="stat-value">{concluidos}</div><div class="stat-label">Atualizadas</div></div>', unsafe_allow_html=True)

st.write("")

# ==============================
# Sidebar — Filtros e Navegação (sem None; já seleciona a 1ª)
# ==============================
st.sidebar.markdown("### Filtros")
somente_pendentes = st.sidebar.toggle("Mostrar apenas 'Não atualizadas'", value=True)
busca = st.sidebar.text_input("Buscar por redacao_id", placeholder="Ex.: 12345")

try:
    df_lista = listar_redacoes(engine, somente_pendentes, busca)
except SQLAlchemyError as e:
    st.error("Erro ao listar redações.")
    st.exception(e)
    st.stop()

ids = df_lista["redacao_id"].tolist()
labels_map = {
    int(rid): f"{int(rid)}  —  {'Não atualizado' if int(status) == 0 else 'Atualizado'}"
    for rid, status in zip(df_lista["redacao_id"], df_lista["status"])
}

if not ids:
    st.sidebar.info("Nenhuma redação encontrada com os filtros atuais.")
    st.info("Ajuste os filtros na barra lateral para exibir redações.")
    st.stop()

# Garante um selecionado válido
if st.session_state.selecionado not in ids:
    st.session_state.selecionado = ids[0]

selecionado_id = st.sidebar.selectbox(
    "Selecione uma redação",
    options=ids,
    index=ids.index(st.session_state.selecionado),
    format_func=lambda rid: labels_map.get(int(rid), str(rid))
)

if selecionado_id != st.session_state.selecionado:
    st.session_state.selecionado = selecionado_id
    st.session_state.loaded_redacao_id = None

# Navegação rápida
st.sidebar.markdown("### Navegação rápida")
idx_atual = ids.index(st.session_state.selecionado)
disabled_prev = idx_atual <= 0
disabled_next = idx_atual >= len(ids) - 1

col_prev, col_next = st.sidebar.columns(2)
with col_prev:
    if st.button("⟵ Anterior", use_container_width=True, disabled=disabled_prev):
        st.session_state.selecionado = ids[idx_atual - 1]
        st.session_state.loaded_redacao_id = None
        st.rerun()
with col_next:
    if st.button("Próximo ⟶", use_container_width=True, disabled=disabled_next):
        st.session_state.selecionado = ids[idx_atual + 1]
        st.session_state.loaded_redacao_id = None
        st.rerun()

st.sidebar.caption("Dica: foque nas 'Não atualizadas' para acelerar a revisão.")

st.write("")

# ==============================
# Painel principal
# ==============================
st.markdown(
    '<div class="toolbar">'
    '<div><strong>Conferência de Redação</strong></div>'
    '<div style="display:flex;gap:.5rem;align-items:center;">'
    '<span class="badge">Clique/roda: zoom • arraste: mover</span>'
    '<span class="badge">Edite o texto à direita</span>'
    '</div></div>',
    unsafe_allow_html=True
)
st.write("")

# Carrega dados da redação selecionada
try:
    dados = carregar_redacao(engine, st.session_state.selecionado)
except Exception as e:
    st.error("Não foi possível carregar a redação selecionada.")
    st.exception(e)
    st.stop()

# Atualiza o editor somente quando muda a redação
if st.session_state.loaded_redacao_id != dados["redacao_id"]:
    st.session_state.texto_digitado_input = dados["texto_digitado"] or ""
    st.session_state.last_saved_text = dados["texto_digitado"] or ""
    st.session_state.loaded_redacao_id = dados["redacao_id"]

col_esq, col_dir = st.columns([1, 1])

# --------- Esquerda: Imagem com zoom ----------
with col_esq:
    st.markdown("#### Imagem da redação")
    if dados["imagem_url"]:
        render_click_zoom(
            image_url=dados["imagem_url"],
            height_px=760,
            step=0.5,
            max_scale=4.0,
            min_scale=1.0
        )
    else:
        st.warning("Nenhuma imagem associada a este redacao_id.")

# --------- Direita: Editor + Status + Salvar ----------
with col_dir:
    st.markdown("#### Texto digitado (editável)")
    status_db = int(dados["status"])
    if status_db == 1:
        st.markdown('<span class="badge status-ok">Atualizado</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="badge status-warn">Não atualizado</span>', unsafe_allow_html=True)

    st.text_area(
        "Edite abaixo e clique em Salvar",
        key="texto_digitado_input",
        height=520,
        help="O texto deve espelhar a redação exibida na imagem ao lado."
    )

    colf1, colf2 = st.columns([1,1])
    if colf1.button("💾 Salvar (marca como 'Atualizado')", use_container_width=True):
        try:
            curr_text = st.session_state.get("texto_digitado_input", "")
            salvar_texto(engine, dados["redacao_id"], curr_text, dados["imagem_url"])
            st.session_state.last_saved_text = curr_text
            st.toast("Salvo com sucesso! Status atualizado para 'Atualizado'.", icon="✅")
            st.rerun()  # garante atualização imediata de status/contadores
        except SQLAlchemyError as e:
            st.error("Erro ao salvar no banco de dados.")
            st.exception(e)

    if colf2.button("✅ Salvar e ir para o próximo", use_container_width=True):
        try:
            curr_text = st.session_state.get("texto_digitado_input", "")
            salvar_texto(engine, dados["redacao_id"], curr_text, dados["imagem_url"])
            st.session_state.last_saved_text = curr_text
            # vai para o próximo
            if st.session_state.selecionado in ids:
                idx = ids.index(st.session_state.selecionado)
                if idx < len(ids) - 1:
                    st.session_state.selecionado = ids[idx + 1]
                    st.session_state.loaded_redacao_id = None
            st.rerun()
        except SQLAlchemyError as e:
            st.error("Erro ao salvar no banco de dados.")
            st.exception(e)

    st.caption(
        f"Caracteres: {len(st.session_state.texto_digitado_input)} • "
        f"Linhas: {st.session_state.texto_digitado_input.count(chr(10)) + 1}"
    )

# Rodapé
st.write("---")
st.caption("CorreigeAI • Conferência de redações — credenciais do banco lidas via st.secrets/.env (não exibidas na interface).")
