"""Скачивает материалы LinQ (SDK и примеры API) с публичных ссылок организаторов.

Ссылки — из «Процедура_подготовки_и_инференса_сетей_на_LinQ_TPU.pdf». Архив папки целиком сервер
не отдаёт, поэтому файлы забираются по одному через WebDAV публичной ссылки Nextcloud.

    python linq/fetch_materials.py [--list] [--only usage_example_python_api.zip tpu_framework.pdf]

Файлы ложатся в linq/downloads/<Framework|API>/ (в .gitignore). Уже скачанные пропускаются.
"""

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

import httpx

SHARES = {"Framework": "bEqr922zr6NXHy4", "API": "QcFLdkdLb71BOmm"}
DAV = "https://cloud.lin-q.ru/public.php/webdav/"
ROOT = Path(__file__).resolve().parent / "downloads"


def listing(client: httpx.Client, token: str, path: str = "") -> list[tuple[str, int | None]]:
    """Рекурсивный список (путь, размер) — сервер поддерживает только Depth: 1."""
    r = client.request("PROPFIND", DAV + urllib.parse.quote(path), auth=(token, ""),
                       headers={"Depth": "1"})  # fmt: skip
    r.raise_for_status()
    items = []
    for resp in r.text.split("<d:response>")[1:]:
        href = urllib.parse.unquote(re.search(r"<d:href>([^<]+)", resp).group(1))
        rel = href.replace("/public.php/webdav/", "").strip("/")
        if rel == path.strip("/"):
            continue
        size = re.search(r"<d:getcontentlength>(\d+)", resp)
        if "<d:collection/>" in resp:
            items.extend(listing(client, token, rel + "/"))
        else:
            items.append((rel, int(size.group(1)) if size else None))
    return items


CHUNK = 8 << 20


def download(client: httpx.Client, token: str, rel: str, size: int | None, dest: Path) -> None:
    """Кусками через Range с докачкой: цельный GET этот сервер не отдаёт (зависает)."""
    done = dest.stat().st_size if dest.exists() else 0
    with dest.open("ab") as fh:
        while size is None or done < size:
            end = done + CHUNK - 1 if size is None else min(done + CHUNK, size) - 1
            for attempt in range(5):
                try:
                    r = client.get(DAV + urllib.parse.quote(rel), auth=(token, ""),
                                   headers={"Range": f"bytes={done}-{end}"})  # fmt: skip
                    r.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    print(f"    повтор {attempt + 1}: {exc}")
            else:
                raise RuntimeError(f"Не удалось скачать {rel} с позиции {done}")
            if not r.content:
                break
            fh.write(r.content)
            done += len(r.content)
            print(f"    {done / 1e6:7.1f} / {(size or 0) / 1e6:.1f} МБ", end="\r")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--list", action="store_true", help="Только показать файлы")
    parser.add_argument("--only", nargs="*", help="Скачать только файлы с такими именами")
    parser.add_argument("--max-mb", type=float, default=2000, help="Не качать файлы больше")
    args = parser.parse_args()

    with httpx.Client(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as client:
        for share, token in SHARES.items():
            files = listing(client, token)
            print(f"== {share}: {len(files)} файлов")
            for rel, size in files:
                mb = (size or 0) / 1e6
                print(f"  {mb:9.1f} МБ  {rel}")
                if args.list or (args.only and Path(rel).name not in args.only) or mb > args.max_mb:
                    continue
                dest = ROOT / share / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                download(client, token, rel, size, dest)
                print(f"             -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
