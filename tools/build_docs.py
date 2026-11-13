# -*- coding: utf-8 -*-
"""
重建「产品资料中心」页面里的文档列表区块。

- 扫描 pdf/ 下的所有 PDF，只重写 index.html 中 <!-- DOCS:START --> 与 <!-- DOCS:END -->
  之间的内容；样式、logo、微信预览逻辑等其余部分原样保留，页面外观不变。
- 既可以在本地手动运行，也由 .github/workflows/update-docs.yml 在 push 到 pdf/ 时自动调用。

日期规则（依次降级）：
  1. 资料说明.json 里手写的“日期”
  2. 该 PDF 在 git 里最后一次提交的日期（CI 必须靠它：checkout 出来的文件时间都是构建时刻）
  3. 文件自身的修改时间 / 创建时间中较新的那个

副标题规则（依次降级）：
  1. 资料说明.json 里手写的“说明”
  2. PDF 属性里的 Subject / Title / Keywords（剔除 Word 等软件生成的无意义值）
  3. 首页正文里挑一到两行像副标题的文字（封面页、目录页自动跳过）
  4. 兜底显示“PDF 文档 · 体积 · 页数”

用法：
  python tools/build_docs.py               # 写回 index.html（内容没变则不写）
  python tools/build_docs.py --check       # 只判断是否需要变更，退出码 1 表示「有变化」
  python tools/build_docs.py --out x.html  # 输出到指定文件，不碰 index.html
  python tools/build_docs.py --no-cover    # 不渲染封面，只出 PDF 图标（快，用于排查）
"""

import base64
import io
import json
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    import pymupdf
except Exception:
    pymupdf = None
try:
    from PIL import Image
except Exception:
    Image = None

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "pdf"
INDEX = ROOT / "index.html"

START = "<!-- DOCS:START -->"
END = "<!-- DOCS:END -->"

COVER_W = 220   # 封面渲染宽度：显示约 92px，2 倍屏够用，体积小可直接内嵌
COVER_Q = 72    # WebP 质量
SKIP_PREFIX = ("_", ".")

# PDF 已迁移到阿里云 OSS（国内对象存储，解决 github.io 国内链路慢），
# 生成的链接指向 OSS 公共读地址；文件名按 UTF-8 百分号编码。
OSS_BASE = "https://fbq-docs-2026.oss-cn-shanghai.aliyuncs.com/pdf/"

META_CANDIDATES = (
    ROOT / "资料说明.json",
    ROOT / "一键更新" / "资料说明.json",
)


def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_attr(text):
    return esc(text).replace('"', "&quot;")


def size_text(num_bytes):
    if num_bytes >= 100 * 1024:
        return "%.1f MB" % (num_bytes / 1024.0 / 1024.0)
    return "%d KB" % max(1, int(round(num_bytes / 1024.0)))


def date_text(dt):
    return dt.strftime("%Y-%m-%d")


def file_date(path):
    stat = path.stat()
    return datetime.fromtimestamp(max(stat.st_mtime, stat.st_ctime))


_git_cache = {}


def git_date(rel):
    """该文件在 git 里最后一次提交的日期；不在 git 里（或没有 git）时返回 None。"""
    if rel in _git_cache:
        return _git_cache[rel]
    value = None
    if (ROOT / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "-c", "core.quotepath=false", "log", "-1", "--format=%cs", "--", "pdf/" + rel],
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            )
            line = (proc.stdout or "").strip().splitlines()
            if line and re.match(r"^\d{4}-\d{2}-\d{2}", line[0]):
                value = datetime.strptime(line[0][:10], "%Y-%m-%d")
        except Exception:
            value = None
    _git_cache[rel] = value
    return value


def parse_date(text):
    """资料说明.json 里的日期，支持 2026-11-09 / 2026/11/9 / 2026.11.9 三种写法。"""
    if not text:
        return None
    value = str(text).strip().replace("/", "-").replace(".", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def norm_key(text):
    return re.sub(r"[\s_\-—·.]+", "", Path(str(text)).stem).lower()


def find_meta():
    for path in META_CANDIDATES:
        if path.exists():
            return path
    return None


def load_meta():
    """读取资料说明.json：{"文档": [{"文件名":..., "标题":..., "说明":..., "日期":...}]}"""
    meta_path = find_meta()
    if meta_path is None:
        return []
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print("[WARN] %s 解析失败，这一条按文件时间处理：%s" % (meta_path.name, exc))
        return []

    items = data.get("文档") if isinstance(data, dict) else data
    result = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("文件名") or "").strip().replace("\\", "/")
            if not name:
                continue
            result.append(
                {
                    "file": name,
                    "title": str(item.get("标题") or "").strip(),
                    "subtitle": str(item.get("说明") or "").strip(),
                    "date": parse_date(item.get("日期")),
                }
            )
    return result


def scan_pdfs():
    found = {}
    for path in sorted(PDF_DIR.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() != ".pdf":
            continue
        if path.name.startswith(SKIP_PREFIX):
            continue
        found[path.relative_to(PDF_DIR).as_posix()] = path
    return found


def build_entries(pdfs, meta):
    """资料说明.json 里列出的按顺序排前，其余按日期倒序追加。"""
    used = set()
    entries = []

    for item in meta:
        key = item["file"]
        path = pdfs.get(key)
        if path is None:
            target = Path(key).name.lower()
            for rel, candidate in pdfs.items():
                if candidate.name.lower() == target and rel not in used:
                    key, path = rel, candidate
                    break
        if path is None:
            target = norm_key(key)
            for rel, candidate in pdfs.items():
                if norm_key(rel) == target and rel not in used:
                    key, path = rel, candidate
                    break
        if path is None or key in used:
            print("[WARN] 资料说明.json 里的“%s”在 pdf 文件夹中找不到，已跳过。" % item["file"])
            continue
        used.add(key)
        entries.append(
            {
                "rel": key,
                "path": path,
                "title": item["title"] or path.stem,
                "subtitle": item["subtitle"],
                "date": item["date"] or git_date(key) or file_date(path),
            }
        )

    rest = [rel for rel in pdfs if rel not in used]
    rest.sort(key=lambda rel: (git_date(rel) or file_date(pdfs[rel]), rel), reverse=True)
    for rel in rest:
        path = pdfs[rel]
        entries.append(
            {
                "rel": rel,
                "path": path,
                "title": path.stem,
                "subtitle": "",
                "date": git_date(rel) or file_date(path),
            }
        )
    return entries


NOISE_LINES = {
    "封面", "封底", "扉页", "目录", "目次", "contents", "content", "index", "ver", "version",
}
NOISE_PATTERN = re.compile(
    r"^(封面|封底|扉页|目录|目次|contents?|index|ver(\.|\s|$)|版本|第\s*\d+\s*页|page\s*\d+)$",
    re.I,
)
BAD_META = re.compile(r"(microsoft|untitled|无标题|word\s*document|doc\d+|presentation|powerpoint)", re.I)


def squash(text, limit=40):
    value = re.sub(r"\s+", " ", str(text)).strip().strip("·-—_,;；。")
    if len(value) > limit:
        value = value[:limit].rstrip("·-—_,;；。 ") + "…"
    return value


def pdf_doc(path):
    if pymupdf is None:
        return None
    try:
        return pymupdf.open(str(path))
    except Exception:
        return None


def meta_subtitle(doc):
    """PDF 属性里的 Subject / Title / Keywords，剔除软件自动生成的无意义值。"""
    if doc is None:
        return ""
    try:
        meta = doc.metadata or {}
    except Exception:
        return ""
    for key in ("subject", "title", "keywords"):
        value = squash(meta.get(key) or "")
        if len(value) >= 4 and not BAD_META.search(value):
            return value
    return ""


def text_subtitle(doc):
    """首页（必要时第 2 页）里挑一两行像副标题的正文。封面/目录页直接跳过。"""
    if doc is None:
        return ""
    try:
        total = min(2, doc.page_count)
    except Exception:
        return ""
    for page_index in range(total):
        try:
            raw = doc[page_index].get_text() or ""
        except Exception:
            continue
        lines = [squash(s) for s in raw.splitlines()]
        lines = [s for s in lines if s]
        if not lines:
            continue
        # 整页只有“封面/目录/Ver.xxx”这类标记，或是目录页，不作为副标题来源
        if NOISE_PATTERN.match(lines[0]) or lines[0] in NOISE_LINES:
            continue
        picked = []
        for line in lines:
            if len(line) < 4 or len(line) > 24:
                continue
            if NOISE_PATTERN.match(line) or line in NOISE_LINES:
                continue
            if re.fullmatch(r"[0-9\W]+", line):
                continue
            if "©" in line or "copyright" in line.lower():
                continue
            # 版本 / 日期这类版式信息不当副标题
            if re.match(r"^(版本|版次|修订|ver\.|v\d|20\d\d)", line, re.I):
                continue
            if re.search(r"(版本|修订|编制|日期)\s*[：:]", line):
                continue
            if line not in picked:
                picked.append(line)
            if len(picked) == 2:
                break
        if picked:
            return squash(" · ".join(picked))
    return ""


def page_count(doc):
    try:
        return doc.page_count if doc is not None else 0
    except Exception:
        return 0


def entry_subtitle(entry):
    """副标题：手写说明优先 → PDF 属性 → 首页正文 → “PDF 文档 · 体积 · 页数”。"""
    if entry["subtitle"]:
        return entry["subtitle"]
    if "auto" in entry:
        return entry["auto"]
    doc = entry.get("doc") or pdf_doc(entry["path"])
    value = meta_subtitle(doc) or text_subtitle(doc)
    if not value:
        value = "PDF 文档 · %s" % size_text(entry["path"].stat().st_size)
        pages = page_count(doc)
        if pages:
            value += " · %d 页" % pages
    entry["auto"] = value
    return value


def cover_data_uri(pdf_path):
    """PDF 首页渲染成小封面，直接内嵌 data: 地址，不落盘。缺依赖时返回 None。"""
    if pymupdf is None or Image is None:
        return None
    try:
        doc = pymupdf.open(str(pdf_path))
        page = doc[0]
        zoom = COVER_W / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=COVER_Q, method=4)
        doc.close()
        return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:
        print("[WARN] 封面生成失败（%s）：%s" % (pdf_path.name, exc))
        return None


def render_body(entries, with_cover=True):
    if not entries:
        return ['  <p class="empty">pdf 文件夹里还没有 PDF 文件。放入文件后这里会自动出现文档卡片。</p>']

    newest = max(entry["date"] for entry in entries)
    lines = [
        '  <p class="doc-count">共 %d 份文档 · 最近更新 %s</p>' % (len(entries), date_text(newest)),
        '  <div class="documents">',
    ]

    for index, entry in enumerate(entries):
        if index:
            lines.append("")
        href = OSS_BASE + urllib.parse.quote(entry["rel"])
        href_attr = esc_attr(href)
        # 磁盘上是原文件名，另存到本地时用页面标题命名
        download_name = re.sub(r'[\\/:*?"<>|]', "-", entry["title"]).strip() or entry["path"].stem
        if not download_name.lower().endswith(".pdf"):
            download_name += ".pdf"
        name_attr = esc_attr(download_name)
        # view=Fit：打开即整页可见、左右留白居中，不会被拉满整屏宽
        preview_attr = href_attr + "#page=1&amp;view=Fit"

        cover = cover_data_uri(entry["path"]) if with_cover else None
        if cover is not None:
            thumb = (
                '    <div class="cover"><img src="%s" alt="%s 首页" '
                'decoding="async" width="%d"><span class="cover-tag">PDF</span></div>'
                % (esc_attr(cover), esc_attr(entry["title"]), COVER_W)
            )
        else:
            thumb = '    <div class="pdf-icon" aria-hidden="true">PDF</div>'

        # 体积与页数：让用户点开前就知道等待成本，与日期合并在同一行
        meta = size_text(entry["path"].stat().st_size)
        pages = page_count(entry.get("doc") or pdf_doc(entry["path"]))
        if pages:
            meta += " · %d 页" % pages
        date_line = date_text(entry["date"]) + " · " + meta

        lines.extend(
            [
                '   <article class="document-card">',
                thumb,
                '    <div class="document-info">',
                '     <h2 class="document-title">%s</h2>' % esc(entry["title"]),
                '     <p class="document-subtitle">%s</p>' % esc(entry_subtitle(entry)),
                '     <p class="document-date">%s</p>' % esc(date_line),
                '     <div class="actions">',
                '      <a class="action action-primary" href="%s" target="_blank" rel="noopener">在线预览</a>'
                % preview_attr,
                '      <a class="action action-secondary" href="%s" download="%s" data-pdf="%s" data-name="%s">下载 PDF</a>'
                % (href_attr, name_attr, href_attr, name_attr),
                "     </div>",
                "    </div>",
                "   </article>",
            ]
        )

    lines.append("  </div>")
    return lines


def read_index():
    raw = INDEX.read_bytes()
    text = raw.decode("utf-8-sig")
    return text.replace("\r\n", "\n"), ("\r\n" in text)


def write_file(path, text, crlf):
    data = text.replace("\r\n", "\n")
    if crlf:
        data = data.replace("\n", "\r\n")
    path.write_bytes(data.encode("utf-8"))


def main(argv):
    check_only = "--check" in argv
    with_cover = "--no-cover" not in argv
    out_path = None
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            print("[ERROR] --out 后面要跟文件名")
            return 2
        out_path = Path(argv[idx + 1])

    if not PDF_DIR.is_dir():
        print("[ERROR] 找不到 pdf 文件夹：%s" % PDF_DIR)
        return 1
    if not INDEX.exists():
        print("[ERROR] 找不到 index.html：%s" % INDEX)
        return 1

    if pymupdf is None or Image is None:
        print("[WARN] 缺 PyMuPDF / Pillow，本次不生成封面，卡片会退回 PDF 图标。")
        print("       安装：python -m pip install pymupdf pillow")

    entries = build_entries(scan_pdfs(), load_meta())
    html, crlf = read_index()

    if html.count(START) != 1 or html.count(END) != 1:
        print("[ERROR] index.html 里 %s / %s 标记缺失或不唯一（%d / %d），为免误改已中止。"
              % (START, END, html.count(START), html.count(END)))
        return 1

    body = "\n".join(render_body(entries, with_cover))
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    new_html = pattern.sub(lambda _m: START + "\n" + body + "\n  " + END, html, count=1)

    changed = new_html != html
    print("[INFO] 扫描到 %d 份 PDF，列表%s。" % (len(entries), "有变化" if changed else "没有变化"))
    for entry in entries:
        print("     - %s  <-  pdf/%s  (%s)" % (entry["title"], entry["rel"], date_text(entry["date"])))

    if out_path is not None:
        write_file(out_path, new_html, crlf)
        print("[OK] 已输出到 %s" % out_path)
        return 0

    if check_only:
        return 1 if changed else 0

    if not changed:
        print("[OK] index.html 无需更新")
        return 0

    write_file(INDEX, new_html, crlf)
    print("[OK] 已写回 %s" % INDEX)
    return 0


if __name__ == "__main__":
    sys.exit(main([a for a in sys.argv[1:]]))
