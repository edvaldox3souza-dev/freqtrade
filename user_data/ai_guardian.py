"""
AI Guardian — Camada de Veto e Convicção com Claude Fable 5.1 (Mirai API) para Freqtrade.
Permite ligar/desligar, editar chaves, ajustar convicção e testar conexão 100% pelo Telegram.
"""
import json
import logging
import os
import pathlib
import threading
import time
import urllib.request
import re

logger = logging.getLogger("ai_guardian")

# Localização do arquivo de estado em disco (persistência robusta no container e no host)
STATE_PATHS = [
    pathlib.Path("/freqtrade/user_data/ai_guardian_state.json"),
    pathlib.Path("user_data/ai_guardian_state.json"),
]

DEFAULT_STATE = {
    "enabled": True,
    "api_key": "sk-sNU4kc9k0F5g97VUVnpNLO9K7pL70mJIYHnkMgSbaz4X8TM7",
    "endpoint": "https://api.miraiapi.com/v1/chat/completions",
    "model": "claude-fable-5.1",
    "min_conviccao": 0.65,
    "last_verdict": None,
    "total_queries": 0,
    "total_vetoes": 0,
    "total_approvals": 0,
}

_STATE_LOCK = threading.RLock()


def _get_target_path() -> pathlib.Path:
    for p in STATE_PATHS:
        try:
            if p.parent.exists():
                return p
        except Exception:
            pass
    return STATE_PATHS[1]


def get_state() -> dict:
    with _STATE_LOCK:
        p = _get_target_path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return {**DEFAULT_STATE, **data}
            except Exception:
                pass
        return dict(DEFAULT_STATE)


def update_state(**kwargs) -> dict:
    with _STATE_LOCK:
        p = _get_target_path()
        current = get_state()
        for k, v in kwargs.items():
            current[k] = v
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:
            logger.warning("[AI GUARDIAN] Falha ao salvar estado: %s", exc)
        return current


def mask_key(k: str) -> str:
    if not k:
        return "NÃO CONFIGURADA"
    if len(k) <= 8:
        return "********"
    return k[:5] + "..." + k[-4:]


def consult_ai_guardian(
    pair: str,
    rate: float,
    ker: float,
    atr_pct: float,
    vol_z: float,
    regime_bull: bool,
    daily_drawdown_pct: float,
    mode: str = "RenderQuantV1",
) -> dict:
    """
    Consulta o Claude Fable 5.1 via Mirai API como Árbitro de Convicção.
    Retorno garantido sem exceção:
    {"aprovado": bool, "conviccao": float, "motivo": str, "origem": "ia" | "fallback" | "bypass"}
    """
    s = get_state()

    # 1. Bypass imediato se desativado pelo operador no Telegram
    if not s.get("enabled", False):
        return {
            "aprovado": True,
            "conviccao": 1.0,
            "motivo": "IA Guardian pausada no Telegram",
            "origem": "bypass",
        }

    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    model = s.get("model", "claude-fable-5.1")
    min_conv = float(s.get("min_conviccao", 0.65))

    if not api_key or not endpoint:
        return {
            "aprovado": True,
            "conviccao": 1.0,
            "motivo": "Chave da IA não configurada (modo passivo)",
            "origem": "bypass",
        }

    # Prompt estruturado para retorno estrito em JSON
    prompt_content = (
        "Você é o Chief Risk Officer AI (Guardião de Risco Cognitivo) de um fundo quantitativo.\n"
        "O motor matemático do Freqtrade pré-aprovou uma entrada com as seguintes métricas de mercado:\n\n"
        f"• Par: {pair}\n"
        f"• Cotação Atual: {rate}\n"
        f"• KER (Kaufman Efficiency Ratio): {ker:.4f} (Ruído < 0.35 | Tendência Limpa >= 0.35)\n"
        f"• ATR Percentual: {atr_pct*100:.2f}%\n"
        f"• Volume Z-Score (MAD Baleia): {vol_z:.2f}\n"
        f"• Regime de Mercado: {'BULL EXPANSÃO' if regime_bull else 'LATERAL/CONSOLIDAÇÃO'}\n"
        f"• Drawdown Diário Atual: {daily_drawdown_pct*100:.2f}%\n"
        f"• Modo Operacional: {mode}\n\n"
        "Avalie se este trade tem alta convicção ou se aparenta falso rompimento/exaustão.\n"
        "RESPONDA ESTRITAMENTE EM JSON VÁLIDO no seguinte formato (sem texto antes ou depois):\n"
        '{"aprovado": true, "conviccao": 0.80, "motivo": "Justificativa analítica concisa em até 20 palavras"}'
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_content}],
        "temperature": 0.1,
        "max_tokens": 150,
    }

    try:
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Freqtrade-AIGuardian/1.0",
            },
        )
        # Timeout calibrado de 10.0s (tempo médio de resposta do Claude Fable via Mirai é ~4.2s)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            raw_body = resp.read().decode("utf-8")
            data = json.loads(raw_body)
            content = data["choices"][0]["message"]["content"].strip()

            # Extrai o bloco JSON usando regex defensivo
            json_match = re.search(r"\{.*\}", content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group(0))
                aprovado_ia = bool(parsed.get("aprovado", True))
                conviccao = float(parsed.get("conviccao", 0.70))
                motivo = str(parsed.get("motivo", "Aprovado por confluência"))

                # Validação contra o patamar de convicção mínima
                aprovado_final = aprovado_ia and (conviccao >= min_conv)
                if not aprovado_final:
                    if not aprovado_ia:
                        motivo = f"VETO IA: {motivo}"
                    else:
                        motivo = f"VETO CONVICÇÃO: {conviccao*100:.0f}% < Mínima {min_conv*100:.0f}% ({motivo})"

                verdict = {
                    "aprovado": aprovado_final,
                    "conviccao": round(conviccao, 2),
                    "motivo": motivo,
                    "origem": "ia",
                    "pair": pair,
                    "ts": time.strftime("%H:%M:%S UTC"),
                }

                # Atualiza métricas
                s["total_queries"] = s.get("total_queries", 0) + 1
                if aprovado_final:
                    s["total_approvals"] = s.get("total_approvals", 0) + 1
                else:
                    s["total_vetoes"] = s.get("total_vetoes", 0) + 1
                s["last_verdict"] = verdict
                update_state(**s)
                return verdict

    except urllib.error.URLError as e:
        logger.warning("[AI GUARDIAN] Timeout/Rede (%s) -> Fallback liberal seguro", e)
    except Exception as exc:
        logger.warning("[AI GUARDIAN] Exceção na consulta (%s) -> Fallback liberal seguro", exc)

    # Fallback seguro: se a API do Mirai falhar, não bloqueia o trade do motor matemático
    verdict = {
        "aprovado": True,
        "conviccao": 0.50,
        "motivo": "Bypass defensivo (Timeout de API)",
        "origem": "fallback",
        "pair": pair,
        "ts": time.strftime("%H:%M:%S UTC"),
    }
    update_state(last_verdict=verdict)
    return verdict


def test_ai_connection() -> tuple[bool, str]:
    """Testa a conectividade da API com latência real medida em milissegundos."""
    s = get_state()
    api_key = s.get("api_key", "").strip()
    endpoint = s.get("endpoint", "").strip()
    if not api_key:
        return False, "❌ Chave de API não informada."
    if not endpoint:
        return False, "❌ Endpoint não configurado."

    t0 = time.time()
    try:
        payload = {
            "model": s.get("model", "claude-fable-5.1"),
            "messages": [{"role": "user", "content": 'Responda estritamente em JSON: {"status": "ok"}'}],
            "max_tokens": 30,
        }
        req_data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=req_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "Freqtrade-AIGuardian/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            latency_ms = int((time.time() - t0) * 1000)
            return True, f"✅ Conexão OK! Modelo: `{s.get('model')}` | Latência: `{latency_ms}ms`"
    except Exception as exc:
        return False, f"❌ Erro de conexão ({exc})"


def format_ai_guardian_text() -> str:
    """Gera o painel visual formatado em Markdown para o Telegram."""
    s = get_state()
    enabled = s.get("enabled", False)
    status_emoji = "🟢 *ATIVO (Veto Cognitivo Ligado)*" if enabled else "🔴 *PAUSADO (Bypass Geral)*"
    min_conv = int(s.get("min_conviccao", 0.65) * 100)
    key_masked = mask_key(s.get("api_key", ""))
    model = s.get("model", "claude-fable-5.1")

    queries = s.get("total_queries", 0)
    approvals = s.get("total_approvals", 0)
    vetoes = s.get("total_vetoes", 0)

    last_v = s.get("last_verdict")
    if last_v and isinstance(last_v, dict):
        status_v = "✅ Aprovado" if last_v.get("aprovado") else "⛔ VETADO"
        pair_v = last_v.get("pair", "N/A")
        conv_v = int(last_v.get("conviccao", 0) * 100)
        motivo_v = last_v.get("motivo", "N/A")
        ts_v = last_v.get("ts", "")
        last_str = f"{status_v} em `{pair_v}` ({conv_v}%) às `{ts_v}`\n↳ _{motivo_v}_"
    else:
        last_str = "_Aguardando primeiro sinal de entrada do robô..._"

    ts_now = time.strftime("%H:%M:%S")

    return (
        f"🧠 *Guardião de Risco Cognitivo (Claude Fable 5.1)*\n\n"
        f"• *Status:* {status_emoji}\n"
        f"• *Motor Cognitivo:* `{model}`\n"
        f"• *API Key:* `{key_masked}`\n"
        f"• *Convicção Mínima:* `{min_conv}%`\n"
        f"• *Auditoria:* `{queries}` analisadas (`{approvals}` aprovadas | `{vetoes}` vetadas)\n\n"
        f"📋 *Último Parecer:* \n{last_str}\n\n"
        f"🕒 *Atualizado:* `{ts_now}`\n\n"
        f"👇 *Controle a IA em tempo real pelos botões abaixo:*"
    )


def build_ai_guardian_inline_markup():
    """Constrói os botões interativos do Telegram."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    except ImportError:
        class InlineKeyboardButton:
            def __init__(self, text, callback_data=None):
                self.text = text
                self.callback_data = callback_data
        class InlineKeyboardMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

    s = get_state()
    enabled = s.get("enabled", False)
    curr_conv = float(s.get("min_conviccao", 0.65))

    # Botão de Ativar/Pausar
    if enabled:
        toggle_btn = InlineKeyboardButton("⏸️ Pausar IA", callback_data="unk_ai:toggle")
    else:
        toggle_btn = InlineKeyboardButton("▶️ Ativar IA", callback_data="unk_ai:toggle")

    # Botões de patamares de convicção
    conv_options = [0.60, 0.65, 0.70, 0.80]
    conv_btns = []
    for c in conv_options:
        mark = "🎯" if abs(c - curr_conv) < 0.01 else ""
        label = f"{mark} {int(c*100)}%"
        conv_btns.append(InlineKeyboardButton(label.strip(), callback_data=f"unk_ai:conv:{c:.2f}"))

    return InlineKeyboardMarkup([
        [toggle_btn],
        conv_btns,
        [
            InlineKeyboardButton("🔑 Inserir API Key", callback_data="unk_ai:key"),
            InlineKeyboardButton("🧪 Testar Conexão", callback_data="unk_ai:test"),
        ],
        [
            InlineKeyboardButton("📋 Ver Parecer", callback_data="unk_ai:verdict"),
            InlineKeyboardButton("🔄 Atualizar", callback_data="unk_ai:refresh"),
        ],
    ])
