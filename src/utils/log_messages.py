"""Portuguese dashboard text, without changing diagnostic LogRecords.

Callers may use ``extra={"user_message": "..."}`` with an ordinary logger, or
the helpers below. File handlers keep the original message and exception. This
module deliberately translates known failures, rather than hiding all errors.
"""
import json
import logging
import re


_ERROR_MESSAGES = (
    (("source payload conflicts", "source review is required", "resolve the source review"),
     "Os dados do pedido foram alterados após o primeiro envio. A sincronização desse pedido aguarda "
     "conferência para evitar duplicidade. Confira o pedido no sistema de origem e no Velide e solicite o suporte."),
    (("pre-upgrade cds receipt", "original cds submission", "cds_upgrade_unresolved"),
     "Este arquivo CDS já existia antes da atualização, e o envio anterior ainda não foi confirmado. "
     "Confira o pedido no Velide e solicite ao suporte a conferência antes de reenviar."),
    (("database belongs to another", "physical source configuration", "authoritative store binding",
      "native migration manifest", "migrate legacy delivery mappings"),
     "A sincronização está pausada porque a instalação ou a loja configurada precisa ser conferida. "
     "Solicite ao suporte a conferência da configuração e da atualização antes de reenviar pedidos."),
    (("source timezone", "bound source timezone", "naive original receipt", "absolute timestamp"),
     "Não foi possível confirmar o horário original do pedido. Confira o fuso horário da integração "
     "com o suporte antes de reenviar."),
    (("database is locked", "database table is locked", "database is busy"),
     "O armazenamento local está ocupado. Aguarde a próxima tentativa de sincronização; "
     "se o problema continuar, consulte o suporte."),
    (("disk is full", "database or disk is full", "no space left"),
     "Não há espaço para salvar a sincronização neste computador. Libere espaço em disco e tente novamente."),
    (("readonly database", "read-only database", "permission denied", "access is denied", "unable to open database"),
     "Não foi possível acessar os arquivos locais da integração. Confira as permissões da pasta com o suporte."),
    (("database disk image is malformed", "synchronization database is newer"),
     "Não foi possível abrir o histórico local de sincronização. Solicite ao suporte a verificação "
     "dos arquivos e da versão instalada antes de continuar."),
    (("could not persist delivery mapping", "could not save native mapping"),
     "Não foi possível salvar a confirmação da entrega neste computador. Confira a entrega no Velide "
     "e solicite o suporte antes de reenviar."),
    (("conflicting remote delivery mapping", "native order mapping conflicts", "idempotency_conflict"),
     "O pedido está associado a informações diferentes no Velide. Confira a entrega e solicite o suporte "
     "antes de reenviar para evitar duplicidade."),
    (("cancellation is unresolved", "contradicts an acknowledged cancellation"),
     "O cancelamento ainda precisa ser conferido no Velide. Verifique a situação da entrega antes de tentar novamente."),
    (("unauthenticated", "token_expired", "token expired", "http_401", "401 unauthorized", "http 401", "status code 401"),
     "A sessão do Velide expirou. Conecte novamente para continuar a sincronização."),
    (("forbidden", "http_403", "403 forbidden", "http 403", "status code 403"),
     "O Velide não autorizou esta operação. Confira a loja e as permissões de acesso com o responsável."),
    (("timeout", "timed out", "connection refused", "connection reset", "network is unreachable",
      "connecterror", "all connection attempts failed", "getaddrinfo failed", "response lost"),
     "Não foi possível confirmar a comunicação com o Velide. Verifique a conexão com a internet "
     "e acompanhe a sincronização antes de reenviar o pedido."),
    (("http_429", "too many requests", "http 429", "status code 429"),
     "O Velide recebeu muitas solicitações. Aguarde alguns instantes para continuar a sincronização."),
    (("http_500", "http_502", "http_503", "http_504", "service unavailable", "bad gateway",
      "http 500", "http 502", "http 503", "http 504", "status code 500", "status code 502", "status code 503", "status code 504"),
     "O Velide está temporariamente indisponível. Aguarde e acompanhe a sincronização."),
    (("api returned success but no id found",),
     "O Velide respondeu sem a identificação da entrega. Confira se ela foi recebida antes de reenviar."),
    (("session was superseded", "session expired", "stale session"),
     "A sessão de conexão mudou durante a operação. Aguarde a conexão atual para continuar a sincronização."),
)


def _error_text(error):
    if isinstance(error, dict):
        return "{} {}".format(error.get("code", ""), error.get("message", error)).strip()
    return str(error)


def _known_error(error):
    text = _error_text(error).lower()
    if isinstance(error, BaseException):
        text = type(error).__name__.lower() + ": " + text
    for fragments, message in _ERROR_MESSAGES:
        if any(fragment in text for fragment in fragments):
            return message
    return None


def humanize_error(error):
    """Explain a failure; unknown causes remain visible instead of disappearing."""
    message = _known_error(error)
    if message:
        return message
    text = _error_text(error).strip()
    if text in (message for _, message in _ERROR_MESSAGES) or text.startswith((
            "Não ", "O ", "Os ", "A ", "As ", "Erro ", "Falha ", "Verifique ", "Confira ",
            "Sincronização ", "Pedido ", "Cancelamento ", "Conexão ", "Sessão ", "Ocorreu ")):
        return text
    if text.startswith("{"):
        try:
            value = json.loads(text)
            if isinstance(value, dict) and "message" in value:
                return humanize_error(value)
        except ValueError:
            pass
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # A complete traceback belongs in the diagnostic file. Keep its actual cause
    # on screen, including unknown errors, rather than a stack of implementation details.
    detail = lines[-1] if "Traceback (most recent call last)" in text and lines else (lines[0] if lines else "causa não informada")
    return ("Não foi possível concluir a sincronização. Detalhe: {}. "
            "Se o problema continuar, consulte o suporte com os registros de diagnóstico.").format(detail.rstrip("."))


def dashboard_message(record):
    """Render only the UI copy; never mutate msg, args, exc_info or exc_text."""
    if getattr(record, "user_message", None):
        return str(record.user_message)
    message = record.getMessage()
    if message == "Durable synchronization operation failed":
        error = record.exc_info[1] if record.exc_info else message
        return humanize_error(error)
    # These existing signals already reach the main-screen logger. Translate the
    # technical reason while preserving the operation/order context around it.
    match = re.match(r"(Sincronização pendente(?: \([^)]*\))?|Sincronização pausada|Erro na reconciliação|Erro no rastreamento):\s*(.*)", message, re.S)
    if match:
        prefix, error = match.groups()
        return prefix + ": " + humanize_error(error)
    known = _known_error(message) if record.levelno >= logging.WARNING else None
    if known:
        return known
    if "Traceback (most recent call last)" in message:
        return humanize_error(message)
    return message


_OPERATIONS = {
    "order_received": "Pedido recebido e salvo para sincronização{order}.",
    "order_sent": "Entrega confirmada no Velide{order}.",
    "cancellation_requested": "Cancelamento solicitado{order}. Aguardando confirmação do Velide.",
    "cancellation_confirmed": "Cancelamento confirmado{order}.",
    "cancellation_failed": "Não foi possível confirmar o cancelamento{order}. Confira a entrega no Velide.",
    "route_started": "Entrega saiu para a rota{order}.",
    "route_completed": "Entrega concluída{order}.",
    "retry_scheduled": "Nova tentativa de sincronização agendada{order}.",
}


def log_operation(logger, event, order_label=None, detail=None, level=None):
    """Hook for confirmed service events; order_label is a customer/name/number.

    Emit cancellation_requested when queued, and cancellation_confirmed only
    after confirmation. ``detail`` remains in diagnostic logs, not the UI row.
    """
    message = _OPERATIONS[event].format(order=" ({})".format(order_label) if order_label else "")
    if level is None:
        level = logging.ERROR if event == "cancellation_failed" else logging.INFO
    logger.log(level, "%s | event=%s | detail=%s", message, event, detail,
               extra={"user_message": message})


def log_sync_error(logger, error, order_label=None, context="Sincronização pendente", exc_info=None):
    """Log the full cause and expose a Portuguese action through QLogHandler."""
    prefix = context + (" ({})".format(order_label) if order_label else "")
    logger.error("%s: %s", prefix, error, exc_info=exc_info,
                 extra={"user_message": prefix + ": " + humanize_error(error)})
