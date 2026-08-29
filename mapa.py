#!/usr/bin/env python3
"""Gera MAPA.md: um mapa do repositorio, feito localmente e de graca.

Por que isso existe: a forma mais cara de usar um agente de codigo e deixar ele
descobrir o repositorio sozinho — ele lista pasta, abre arquivo, se perde, abre
de novo. Cada uma dessas idas e vindas queima cota.

Aqui o mapa e extraido por regex local (custo zero) e colado no contexto uma vez
so. O modelo ja comeca sabendo onde as coisas estao.

Uso:
    python3 mapa.py                     # mapeia o diretorio atual
    python3 mapa.py /caminho/do/projeto -o MAPA.md
    python3 mapa.py --max-simbolos 8    # menos verboso, menos tokens
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Extensao -> (nome da linguagem, lista de regex que capturam simbolos de topo)
LINGUAGENS: dict[str, tuple[str, list[str]]] = {
    ".py": ("Python", [
        r"^class\s+(\w+)", r"^def\s+(\w+)", r"^async\s+def\s+(\w+)",
    ]),
    ".js": ("JavaScript", [
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", r"^(?:export\s+)?class\s+(\w+)",
        r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(", r"^module\.exports\.(\w+)",
    ]),
    ".ts": ("TypeScript", [
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", r"^(?:export\s+)?class\s+(\w+)",
        r"^(?:export\s+)?(?:interface|type|enum)\s+(\w+)",
        r"^(?:export\s+)?const\s+(\w+)\s*[:=]",
    ]),
    ".java": ("Java", [
        r"^\s*(?:public|protected|private)?\s*(?:abstract\s+|final\s+)?(?:class|interface|enum|record)\s+(\w+)",
    ]),
    ".go": ("Go", [r"^func\s+(?:\([^)]*\)\s*)?(\w+)", r"^type\s+(\w+)"]),
    ".rb": ("Ruby", [r"^\s*class\s+(\w+)", r"^\s*module\s+(\w+)", r"^\s*def\s+(\w+)"]),
    ".php": ("PHP", [r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", r"^\s*function\s+(\w+)"]),
    ".cs": ("C#", [r"^\s*(?:public|internal|private)?\s*(?:static\s+|abstract\s+|sealed\s+)*(?:class|interface|record|struct|enum)\s+(\w+)"]),
    ".rs": ("Rust", [r"^(?:pub\s+)?fn\s+(\w+)", r"^(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)"]),
    ".sql": ("SQL", [r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:table|view|function|procedure)\s+[`\"\[]?(\w+)"]),
    ".sh": ("Shell", [r"^(?:function\s+)?(\w+)\s*\(\)\s*\{"]),
}
LINGUAGENS[".jsx"] = LINGUAGENS[".js"]
LINGUAGENS[".tsx"] = LINGUAGENS[".ts"]
LINGUAGENS[".mjs"] = LINGUAGENS[".js"]
LINGUAGENS[".cjs"] = LINGUAGENS[".js"]

IGNORAR_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build",
    "target", "out", ".next", ".nuxt", "vendor", ".idea", ".vscode", "coverage",
    ".pytest_cache", ".mypy_cache", ".tox", "bin", "obj", ".gradle", ".terraform",
}
IGNORAR_ARQ = re.compile(
    r"\.(min\.js|min\.css|lock|map|png|jpe?g|gif|svg|ico|pdf|zip|tar|gz|exe|dll|so|"
    r"woff2?|ttf|eot|mp[34]|avi|mov|class|pyc|jar|war)$", re.I,
)

# Arquivos que costumam ser porta de entrada ou fonte de convencao do projeto.
NOTAVEIS = {
    "readme.md", "readme.rst", "contributing.md", "makefile", "dockerfile",
    "docker-compose.yml", "package.json", "pyproject.toml", "setup.py",
    "requirements.txt", "pom.xml", "build.gradle", "go.mod", "cargo.toml",
    "composer.json", "gemfile", "main.py", "app.py", "manage.py", "index.js",
    "index.ts", "main.go", "main.java", "program.cs", "agents.md",
    ".github/copilot-instructions.md", "claude.md",
}

IMPORT_RE = {
    ".py": re.compile(r"^(?:from\s+([\w.]+)|import\s+([\w.]+))"),
    ".js": re.compile(r"""(?:from\s+['"]([^'"]+)['"]|require\(['"]([^'"]+)['"]\))"""),
}
IMPORT_RE[".ts"] = IMPORT_RE[".tsx"] = IMPORT_RE[".jsx"] = IMPORT_RE[".js"]


def listar_arquivos(raiz: Path) -> list[Path]:
    """Prefere o git (respeita .gitignore de graca); cai para os.walk se nao houver."""
    try:
        saida = subprocess.run(
            ["git", "-C", str(raiz), "ls-files"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        arquivos = [raiz / linha for linha in saida.splitlines() if linha.strip()]
        if arquivos:
            return [a for a in arquivos if a.is_file()]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    arquivos = []
    for dirpath, dirnames, filenames in os.walk(raiz):
        dirnames[:] = [d for d in dirnames if d not in IGNORAR_DIRS and not d.startswith(".")]
        for nome in filenames:
            arquivos.append(Path(dirpath) / nome)
    return arquivos


def extrair_python(texto: str, max_simbolos: int) -> tuple[list[str], list[str]] | None:
    """Simbolos e imports de um arquivo Python via ast (pega metodos de classe).

    Retorna None se o arquivo nao parsear (Python 2, template, arquivo quebrado)
    para o chamador cair no caminho de regex.
    """
    try:
        arvore = ast.parse(texto)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None

    simbolos: list[str] = []
    for no in arvore.body:
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not no.name.startswith("_"):
                simbolos.append(no.name)
        elif isinstance(no, ast.ClassDef):
            if no.name.startswith("_"):
                continue
            simbolos.append(no.name)
            for filho in no.body:
                if isinstance(filho, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and not filho.name.startswith("_"):
                    simbolos.append(f"{no.name}.{filho.name}")
    if len(simbolos) > max_simbolos:
        simbolos = simbolos[:max_simbolos] + ["..."]

    imports: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            imports += [alias.name for alias in no.names]
        elif isinstance(no, ast.ImportFrom) and no.module and no.level == 0:
            imports.append(no.module)
    return simbolos, imports


def extrair(caminho: Path, max_simbolos: int) -> tuple[int, list[str], list[str]]:
    """Retorna (linhas, simbolos de topo, imports)."""
    ext = caminho.suffix.lower()
    lingua = LINGUAGENS.get(ext)
    try:
        texto = caminho.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0, [], []

    linhas = texto.count("\n") + 1
    if not lingua:
        return linhas, [], []

    if ext == ".py":
        preciso = extrair_python(texto, max_simbolos)
        if preciso is not None:
            return linhas, preciso[0], preciso[1]

    padroes = [re.compile(p) for p in lingua[1]]
    simbolos: list[str] = []
    for linha in texto.splitlines():
        if len(simbolos) >= max_simbolos:
            simbolos.append("...")
            break
        for pad in padroes:
            m = pad.match(linha)
            if m:
                nome = next((g for g in m.groups() if g), None)
                if nome and not nome.startswith("_") and nome not in simbolos:
                    simbolos.append(nome)
                break

    imports: list[str] = []
    imp_re = IMPORT_RE.get(ext)
    if imp_re:
        for linha in texto.splitlines()[:120]:
            m = imp_re.search(linha)
            if m:
                alvo = next((g for g in m.groups() if g), None)
                if alvo:
                    imports.append(alvo)
    return linhas, simbolos, imports


def humano(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def gerar(raiz: Path, max_simbolos: int, max_arquivos_detalhe: int) -> str:
    arquivos = [
        a for a in listar_arquivos(raiz)
        if not IGNORAR_ARQ.search(a.name)
        and not any(p in IGNORAR_DIRS for p in a.parts)
    ]

    dados = []
    por_lingua: Counter[str] = Counter()
    linhas_por_lingua: Counter[str] = Counter()
    imports_externos: Counter[str] = Counter()
    total_bytes = 0

    for a in arquivos:
        try:
            tam = a.stat().st_size
        except OSError:
            continue
        if tam > 800_000:  # arquivo gigante: conta, mas nao analisa
            continue
        total_bytes += tam
        linhas, simbolos, imports = extrair(a, max_simbolos)
        ext = a.suffix.lower()
        nome_lingua = LINGUAGENS.get(ext, (ext or "(sem extensao)", []))[0]
        por_lingua[nome_lingua] += 1
        linhas_por_lingua[nome_lingua] += linhas
        for imp in imports:
            if not imp.startswith((".", "/")):
                imports_externos[imp.split(".")[0].split("/")[0]] += 1
        dados.append({
            "rel": a.relative_to(raiz).as_posix(),
            "linhas": linhas,
            "simbolos": simbolos,
        })

    dados.sort(key=lambda d: -d["linhas"])
    por_dir: dict[str, list[dict]] = defaultdict(list)
    for d in dados:
        por_dir[str(Path(d["rel"]).parent)].append(d)

    out: list[str] = []
    w = out.append
    w(f"# Mapa de `{raiz.name}`")
    w("")
    w("> Gerado localmente por `mapa.py`. Custo zero de credito.")
    w("> Leia isto antes de sair abrindo arquivo.")
    w("")
    w("## Resumo")
    w("")
    w(f"- Arquivos analisados: **{len(dados)}**")
    w(f"- Linhas de codigo: **{humano(sum(d['linhas'] for d in dados))}**")
    w(f"- Tamanho: **{total_bytes/1024/1024:.1f} MB**")
    w(f"- Estimativa de tokens do repo inteiro: **~{humano(int(total_bytes/3.7))}** "
      "(por isso nao se manda o repo inteiro)")
    w("")
    w("| Linguagem | Arquivos | Linhas |")
    w("|---|---:|---:|")
    for lingua, n in por_lingua.most_common(12):
        w(f"| {lingua} | {n} | {humano(linhas_por_lingua[lingua])} |")
    w("")

    notaveis = [d for d in dados if d["rel"].lower() in NOTAVEIS
                or Path(d["rel"]).name.lower() in NOTAVEIS]
    if notaveis:
        w("## Pontos de entrada e convencoes")
        w("")
        for d in sorted(notaveis, key=lambda d: d["rel"]):
            w(f"- `{d['rel']}` ({d['linhas']} linhas)")
        w("")

    if imports_externos:
        w("## Dependencias mais usadas")
        w("")
        w(", ".join(f"`{nome}` ({n}x)" for nome, n in imports_externos.most_common(15)))
        w("")

    w("## Arquivos maiores")
    w("")
    w("| Arquivo | Linhas |")
    w("|---|---:|")
    for d in dados[:15]:
        w(f"| `{d['rel']}` | {d['linhas']} |")
    w("")

    w("## Estrutura e simbolos")
    w("")
    detalhados = 0
    for dirname in sorted(por_dir):
        arqs = sorted(por_dir[dirname], key=lambda d: -d["linhas"])
        rotulo = "." if dirname == "." else dirname
        total_linhas = sum(a["linhas"] for a in arqs)
        w(f"### `{rotulo}/`  — {len(arqs)} arquivos, {humano(total_linhas)} linhas")
        w("")
        for d in arqs:
            if d["simbolos"]:
                if detalhados < max_arquivos_detalhe:
                    w(f"- `{Path(d['rel']).name}` ({d['linhas']}L): "
                      + ", ".join(f"`{s}`" for s in d["simbolos"]))
                    detalhados += 1
                else:
                    w(f"- `{Path(d['rel']).name}` ({d['linhas']}L)")
            else:
                w(f"- `{Path(d['rel']).name}` ({d['linhas']}L)")
        w("")
    if detalhados >= max_arquivos_detalhe:
        w(f"> Simbolos omitidos apos {max_arquivos_detalhe} arquivos para nao inflar o mapa. "
          "Use `--max-arquivos-detalhe` para mudar.")
        w("")

    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("raiz", nargs="?", default=".", help="raiz do projeto")
    p.add_argument("-o", "--saida", default="MAPA.md")
    p.add_argument("--max-simbolos", type=int, default=12, help="simbolos por arquivo")
    p.add_argument("--max-arquivos-detalhe", type=int, default=200,
                   help="ate quantos arquivos listam simbolos")
    p.add_argument("--stdout", action="store_true", help="imprime em vez de gravar")
    args = p.parse_args()

    raiz = Path(args.raiz).resolve()
    if not raiz.is_dir():
        print(f"erro: {raiz} nao e um diretorio", file=sys.stderr)
        return 1

    texto = gerar(raiz, args.max_simbolos, args.max_arquivos_detalhe)
    if args.stdout:
        print(texto)
    else:
        destino = Path(args.saida)
        destino.write_text(texto, encoding="utf-8")
        tokens = int(len(texto) / 3.7)
        print(f"{destino} gerado: {len(texto)} chars, ~{tokens} tokens de contexto")
    return 0


if __name__ == "__main__":
    sys.exit(main())
