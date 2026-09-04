#!/usr/bin/env python3
"""
Coleta modelos, precos e a cota do usuario autenticado no GitHub Copilot CLI.

Uso normal (coleta imediatamente e repete a cada 3 horas):
    python copilot_uso.py

Somente uma coleta:
    python copilot_uso.py --uma-vez

Opcoes:
    python copilot_uso.py --saida C:\\caminho\\dados --intervalo-horas 3

Requisitos: Python 3 e o comando `copilot` instalado e autenticado.
Nao usa bibliotecas externas, PowerShell, arquivos .cmd ou Agendador de Tarefas.
"""

import argparse
import csv
import ctypes
import datetime as dt
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


VERSAO = "1.0.0"
MAX_RPC_BYTES = 50 * 1024 * 1024
MUTEX_NAME = r"Local\CopilotUsoCsvPython"

MODEL_FIELDS = [
    "coleta_em",
    "modelo_id",
    "nome",
    "estado_politica",
    "categoria",
    "categoria_preco",
    "multiplicador_request",
    "desconto_percentual",
    "lote_tokens",
    "entrada_creditos_por_lote",
    "cache_leitura_creditos_por_lote",
    "cache_escrita_creditos_por_lote",
    "cache_escrita_1h_creditos_por_lote",
    "saida_creditos_por_lote",
    "entrada_usd_por_lote",
    "cache_leitura_usd_por_lote",
    "cache_escrita_usd_por_lote",
    "cache_escrita_1h_usd_por_lote",
    "saida_usd_por_lote",
    "max_entrada_tokens",
    "max_saida_tokens",
    "max_contexto_tokens",
    "suporta_visao",
    "suporta_esforco_raciocinio",
    "esforcos_raciocinio",
    "esforco_padrao",
    "contexto_longo_entrada_creditos",
    "contexto_longo_cache_leitura_creditos",
    "contexto_longo_cache_escrita_creditos",
    "contexto_longo_saida_creditos",
    "contexto_longo_max_entrada_tokens",
    "promocao_desconto_percentual",
    "promocao_termina_em",
]

QUOTA_FIELDS = [
    "coleta_em",
    "tipo_cota",
    "incluido",
    "usado",
    "restante_calculado",
    "restante_percentual",
    "excedente",
    "ilimitado",
    "uso_apos_esgotar_permitido",
    "excedente_apos_esgotar_permitido",
    "reinicia_em",
]

EXECUTION_FIELDS = [
    "coleta_em",
    "status",
    "modelos",
    "cotas",
    "versao_cli",
    "erro",
]


class CollectorError(RuntimeError):
    pass


def agora_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def get_value(obj, name, default=""):
    if not isinstance(obj, dict):
        return default
    value = obj.get(name)
    return default if value is None else value


def joined(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def money_usd(ai_credits):
    if ai_credits in (None, ""):
        return ""
    try:
        return Decimal(str(ai_credits)) * Decimal("0.01")
    except (InvalidOperation, ValueError):
        return ""


def csv_cell(value):
    """Formata numeros com virgula decimal para abrir bem no Excel pt-BR."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text.replace(".", ",")
    if isinstance(value, float):
        return format(value, ".15g").replace(".", ",")
    return value


def normalize_row(row, fields):
    return {field: csv_cell(row.get(field, "")) for field in fields}


def write_current_csv(path, rows, fields):
    if not rows:
        return
    temporary = Path(str(path) + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", lineterminator="\n")
        writer.writeheader()
        writer.writerows(normalize_row(row, fields) for row in rows)
    os.replace(str(temporary), str(path))


def append_history_csv(path, rows, fields):
    if not rows:
        return
    exists = path.exists() and path.stat().st_size > 0
    encoding = "utf-8" if exists else "utf-8-sig"
    with path.open("a", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", lineterminator="\n")
        if not exists:
            writer.writeheader()
        writer.writerows(normalize_row(row, fields) for row in rows)


def append_log(output_dir, message):
    timestamp = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    with (output_dir / "coletor.log").open("a", encoding="utf-8") as handle:
        handle.write("{} {}\n".format(timestamp, message))


class SingleInstance:
    """Impede duas copias do coletor de gravarem nos mesmos CSVs."""

    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.handle = None
        self.lock_file = None

    def acquire(self):
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]

            self.kernel32 = kernel32
            self.handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(self.handle)
                self.handle = None
                return False
            return True

        # Este trecho serve apenas para testes fora do Windows.
        import fcntl

        self.lock_file = (self.output_dir / ".copilot_uso.lock").open("a+")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            return False

    def release(self):
        if os.name == "nt" and self.handle:
            self.kernel32.ReleaseMutex(self.handle)
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
        elif self.lock_file:
            try:
                import fcntl

                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self.lock_file.close()
                self.lock_file = None


def resolve_copilot(requested=""):
    if requested:
        requested_path = Path(requested).expanduser()
        if requested_path.is_file():
            return str(requested_path.resolve())
        found = shutil.which(requested)
        if found:
            return str(Path(found).resolve())
        raise CollectorError("Copilot CLI nao encontrado em: {}".format(requested))

    for command in ("copilot", "copilot.exe", "copilot.cmd"):
        found = shutil.which(command)
        if found:
            return str(Path(found).resolve())
    raise CollectorError(
        "Copilot CLI nao foi encontrado. Abra o mesmo terminal e confira: copilot --version"
    )


def copilot_command(executable, arguments):
    suffix = Path(executable).suffix.lower()
    if suffix in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        inner_command = subprocess.list2cmdline([executable] + list(arguments))
        return [comspec, "/d", "/s", "/c", inner_command]
    if suffix == ".ps1":
        raise CollectorError(
            "O comando copilot encontrado e um script PowerShell. Instale/use a versao executavel do Copilot CLI."
        )
    return [executable] + list(arguments)


def process_options():
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return options


def cli_version(executable):
    try:
        completed = subprocess.run(
            copilot_command(executable, ["--version"]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            **process_options()
        )
        text = completed.stdout.decode("utf-8", errors="replace").strip()
        return text.splitlines()[0] if text else "nao identificada"
    except Exception:
        return "nao identificada"


class RpcClient:
    def __init__(self, executable, timeout_seconds):
        self.timeout = timeout_seconds
        self.next_id = 0
        self.messages = queue.Queue()
        self.stderr_parts = []
        self.write_lock = threading.Lock()

        args = ["--headless", "--stdio", "--no-auto-update", "--log-level", "error"]
        try:
            self.process = subprocess.Popen(
                copilot_command(executable, args),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                **process_options()
            )
        except OSError as exc:
            raise CollectorError("Nao foi possivel iniciar o Copilot CLI: {}".format(exc))

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.reader_thread.start()
        self.stderr_thread.start()

    def _read_exact(self, length):
        chunks = []
        remaining = length
        while remaining:
            chunk = self.process.stdout.read(remaining)
            if not chunk:
                raise EOFError("Resposta incompleta recebida do Copilot CLI.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _reader_loop(self):
        try:
            while True:
                content_length = None
                header_size = 0
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("O Copilot CLI encerrou a conexao antes de responder.")
                    header_size += len(line)
                    if header_size > 16384:
                        raise CollectorError("Cabecalho JSON-RPC grande demais.")
                    if line in (b"\r\n", b"\n"):
                        break
                    name, separator, value = line.decode("ascii", errors="replace").partition(":")
                    if separator and name.strip().lower() == "content-length":
                        content_length = int(value.strip())

                if content_length is None:
                    raise CollectorError("Resposta JSON-RPC sem Content-Length.")
                if content_length < 0 or content_length > MAX_RPC_BYTES:
                    raise CollectorError("Tamanho JSON-RPC invalido: {} bytes.".format(content_length))

                body = self._read_exact(content_length)
                message = json.loads(body.decode("utf-8"))
                self.messages.put(("message", message))
        except Exception as exc:
            self.messages.put(("error", exc))

    def _stderr_loop(self):
        try:
            while True:
                chunk = self.process.stderr.read(4096)
                if not chunk:
                    return
                if sum(len(part) for part in self.stderr_parts) < 200000:
                    self.stderr_parts.append(chunk)
        except Exception:
            return

    def stderr_text(self):
        return b"".join(self.stderr_parts).decode("utf-8", errors="replace").strip()

    def _send(self, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        header = "Content-Length: {}\r\n\r\n".format(len(body)).encode("ascii")
        with self.write_lock:
            try:
                self.process.stdin.write(header + body)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise CollectorError("Conexao com o Copilot CLI foi encerrada: {}".format(exc))

    def call(self, method, parameters=None, timeout=None):
        self.next_id += 1
        request_id = self.next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": parameters or {},
            }
        )

        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollectorError("Tempo esgotado aguardando {}.".format(method))
            try:
                kind, message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise CollectorError("Tempo esgotado aguardando {}.".format(method))

            if kind == "error":
                raise CollectorError(str(message))
            if not isinstance(message, dict):
                continue

            if str(message.get("id")) == str(request_id):
                rpc_error = message.get("error")
                if rpc_error:
                    raise CollectorError(
                        "Falha em {} (RPC {}): {}".format(
                            method,
                            rpc_error.get("code", "?"),
                            rpc_error.get("message", "erro desconhecido"),
                        )
                    )
                return message.get("result")

            # Responde de forma explicita se o servidor fizer uma chamada ao cliente.
            if message.get("id") is not None and message.get("method"):
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Metodo nao implementado pelo coletor",
                        },
                    }
                )

    def close(self):
        if self.process.poll() is None:
            try:
                self.call("runtime.shutdown", {}, timeout=5)
            except Exception:
                pass
        try:
            self.process.stdin.close()
        except Exception:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def models_to_rows(models, collected_at):
    rows = []
    for model in models if isinstance(models, list) else []:
        billing = get_value(model, "billing", {})
        prices = get_value(billing, "tokenPrices", {})
        long_context = get_value(prices, "longContext", {})
        capabilities = get_value(model, "capabilities", {})
        supports = get_value(capabilities, "supports", {})
        limits = get_value(capabilities, "limits", {})
        policy = get_value(model, "policy", {})
        promo = get_value(billing, "promo", {})

        input_price = get_value(prices, "inputPrice")
        cache_read_price = get_value(prices, "cacheReadPrice")
        if cache_read_price == "":
            cache_read_price = get_value(prices, "cachePrice")
        cache_write_price = get_value(prices, "cacheWritePrice")
        cache_write_1h_price = get_value(prices, "cacheWrite1hPrice")
        output_price = get_value(prices, "outputPrice")

        rows.append(
            {
                "coleta_em": collected_at,
                "modelo_id": get_value(model, "id"),
                "nome": get_value(model, "name"),
                "estado_politica": get_value(policy, "state"),
                "categoria": get_value(model, "modelPickerCategory"),
                "categoria_preco": get_value(model, "modelPickerPriceCategory"),
                "multiplicador_request": get_value(billing, "multiplier"),
                "desconto_percentual": get_value(billing, "discountPercent"),
                "lote_tokens": get_value(prices, "batchSize"),
                "entrada_creditos_por_lote": input_price,
                "cache_leitura_creditos_por_lote": cache_read_price,
                "cache_escrita_creditos_por_lote": cache_write_price,
                "cache_escrita_1h_creditos_por_lote": cache_write_1h_price,
                "saida_creditos_por_lote": output_price,
                "entrada_usd_por_lote": money_usd(input_price),
                "cache_leitura_usd_por_lote": money_usd(cache_read_price),
                "cache_escrita_usd_por_lote": money_usd(cache_write_price),
                "cache_escrita_1h_usd_por_lote": money_usd(cache_write_1h_price),
                "saida_usd_por_lote": money_usd(output_price),
                "max_entrada_tokens": get_value(limits, "max_prompt_tokens"),
                "max_saida_tokens": get_value(limits, "max_output_tokens"),
                "max_contexto_tokens": get_value(limits, "max_context_window_tokens"),
                "suporta_visao": get_value(supports, "vision"),
                "suporta_esforco_raciocinio": get_value(supports, "reasoningEffort"),
                "esforcos_raciocinio": joined(get_value(model, "supportedReasoningEfforts")),
                "esforco_padrao": get_value(model, "defaultReasoningEffort"),
                "contexto_longo_entrada_creditos": get_value(long_context, "inputPrice"),
                "contexto_longo_cache_leitura_creditos": get_value(long_context, "cacheReadPrice"),
                "contexto_longo_cache_escrita_creditos": get_value(long_context, "cacheWritePrice"),
                "contexto_longo_saida_creditos": get_value(long_context, "outputPrice"),
                "contexto_longo_max_entrada_tokens": get_value(long_context, "maxPromptTokens"),
                "promocao_desconto_percentual": get_value(promo, "discountPercent"),
                "promocao_termina_em": get_value(promo, "endsAt"),
            }
        )
    return rows


def quota_to_rows(quota_snapshots, collected_at):
    rows = []
    if not isinstance(quota_snapshots, dict):
        return rows

    for quota_type in sorted(quota_snapshots):
        snapshot = quota_snapshots[quota_type]
        entitlement = get_value(snapshot, "entitlementRequests")
        used = get_value(snapshot, "usedRequests")
        remaining = ""
        if (
            isinstance(entitlement, (int, float))
            and not isinstance(entitlement, bool)
            and isinstance(used, (int, float))
            and not isinstance(used, bool)
            and entitlement >= 0
        ):
            remaining = max(0, entitlement - used)

        rows.append(
            {
                "coleta_em": collected_at,
                "tipo_cota": quota_type,
                "incluido": entitlement,
                "usado": used,
                "restante_calculado": remaining,
                "restante_percentual": get_value(snapshot, "remainingPercentage"),
                "excedente": get_value(snapshot, "overage"),
                "ilimitado": get_value(snapshot, "isUnlimitedEntitlement"),
                "uso_apos_esgotar_permitido": get_value(
                    snapshot, "usageAllowedWithExhaustedQuota"
                ),
                "excedente_apos_esgotar_permitido": get_value(
                    snapshot, "overageAllowedWithExhaustedQuota"
                ),
                "reinicia_em": get_value(snapshot, "resetDate"),
            }
        )
    return rows


def collect_once(output_dir, requested_copilot, timeout_seconds):
    output_dir.mkdir(parents=True, exist_ok=True)
    single_instance = SingleInstance(output_dir)
    if not single_instance.acquire():
        print("Coleta ignorada: outra copia do programa ja esta executando.")
        return 0

    collected_at = agora_iso()
    status = "erro"
    errors = []
    model_count = 0
    quota_count = 0
    version = ""

    try:
        executable = resolve_copilot(requested_copilot)
        version = cli_version(executable)

        try:
            with RpcClient(executable, timeout_seconds) as client:
                client.call(
                    "connect",
                    {
                        "clientInfo": {
                            "editorName": "copilot-usage-csv-python",
                            "editorVersion": VERSAO,
                        }
                    },
                )

                try:
                    model_result = client.call("models.list", {})
                    models = get_value(model_result, "models", [])
                    model_rows = models_to_rows(models, collected_at)
                    if not model_rows:
                        raise CollectorError("o runtime nao retornou modelos")
                    write_current_csv(output_dir / "modelos_atuais.csv", model_rows, MODEL_FIELDS)
                    append_history_csv(
                        output_dir / "modelos_historico.csv", model_rows, MODEL_FIELDS
                    )
                    model_count = len(model_rows)
                except Exception as exc:
                    errors.append("modelos: {}".format(exc))

                try:
                    quota_result = client.call("account.getQuota", {})
                    snapshots = get_value(quota_result, "quotaSnapshots", None)
                    quota_rows = quota_to_rows(snapshots, collected_at)
                    if not quota_rows:
                        raise CollectorError("o runtime nao retornou cotas para esta conta")
                    write_current_csv(output_dir / "uso_atual.csv", quota_rows, QUOTA_FIELDS)
                    append_history_csv(
                        output_dir / "uso_historico.csv", quota_rows, QUOTA_FIELDS
                    )
                    quota_count = len(quota_rows)
                except Exception as exc:
                    errors.append("uso: {}".format(exc))

                stderr = client.stderr_text()
                if stderr and model_count == 0 and quota_count == 0:
                    errors.append("copilot: {}".format(" ".join(stderr.splitlines())))
        except Exception as exc:
            errors.append(str(exc))

        if not errors:
            status = "ok"
        elif model_count or quota_count:
            status = "parcial"
        else:
            status = "erro"
    except Exception as exc:
        errors.append(str(exc))
        status = "erro"
    finally:
        error_text = " | ".join(dict.fromkeys(errors))
        execution_row = {
            "coleta_em": collected_at,
            "status": status,
            "modelos": model_count,
            "cotas": quota_count,
            "versao_cli": version,
            "erro": error_text,
        }
        try:
            append_history_csv(
                output_dir / "execucoes.csv", [execution_row], EXECUTION_FIELDS
            )
            append_log(
                output_dir,
                "status={}; modelos={}; cotas={}; erro={}".format(
                    status, model_count, quota_count, error_text
                ),
            )
        finally:
            single_instance.release()

    if status in ("ok", "parcial"):
        print(
            "{} Coleta {}: {} modelos e {} cotas. CSV em: {}".format(
                collected_at, status, model_count, quota_count, output_dir
            )
        )
        if errors:
            print("Aviso: {}".format(" | ".join(errors)))
        return 0

    print("{} Coleta falhou: {}".format(collected_at, " | ".join(errors)))
    print("Consulte: {}".format(output_dir / "coletor.log"))
    return 1


def run_loop(output_dir, requested_copilot, timeout_seconds, interval_hours):
    interval_seconds = interval_hours * 60 * 60
    print("Coletor do GitHub Copilot iniciado.")
    print("Intervalo: {} hora(s). Para parar, pressione Ctrl+C.".format(interval_hours))
    print("CSV: {}".format(output_dir))

    while True:
        cycle_started = time.monotonic()
        collect_once(output_dir, requested_copilot, timeout_seconds)
        elapsed = time.monotonic() - cycle_started
        wait_seconds = max(0, interval_seconds - elapsed)
        next_run = dt.datetime.now().astimezone() + dt.timedelta(seconds=wait_seconds)
        print("Proxima coleta: {}".format(next_run.strftime("%d/%m/%Y %H:%M:%S %z")))
        time.sleep(wait_seconds)


def parse_args():
    default_output = Path(__file__).resolve().parent / "dados_copilot"
    parser = argparse.ArgumentParser(
        description="Coleta modelos, precos e cota do GitHub Copilot em CSV."
    )
    parser.add_argument(
        "--uma-vez",
        action="store_true",
        help="faz uma coleta e encerra, sem o loop de 3 horas",
    )
    parser.add_argument(
        "--intervalo-horas",
        type=float,
        default=3.0,
        help="intervalo do loop em horas (padrao: 3)",
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=default_output,
        help="pasta dos CSVs (padrao: dados_copilot ao lado deste arquivo)",
    )
    parser.add_argument(
        "--copilot",
        default="",
        help="caminho do Copilot CLI; normalmente e detectado automaticamente",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="tempo maximo de cada resposta do CLI, em segundos (padrao: 60)",
    )
    args = parser.parse_args()
    if args.intervalo_horas <= 0:
        parser.error("--intervalo-horas deve ser maior que zero")
    if args.timeout < 5:
        parser.error("--timeout deve ser pelo menos 5 segundos")
    return args


def main():
    args = parse_args()
    output_dir = args.saida.expanduser().resolve()

    try:
        if args.uma_vez:
            return collect_once(output_dir, args.copilot, args.timeout)
        run_loop(
            output_dir,
            args.copilot,
            args.timeout,
            args.intervalo_horas,
        )
        return 0
    except KeyboardInterrupt:
        print("\nColetor encerrado pelo usuario (Ctrl+C).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
