"""Build the local Chinese reading edition from the Markdown sources.

Requires ReportLab, pypdf and svglib. Figure PDFs are embedded as vectors.
This exporter supports the Markdown constructs used by this manuscript; it is
not a general Markdown converter. No model, database, or network is accessed.
"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
import re
import tempfile
from io import BytesIO
from urllib.parse import quote, unquote

from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

PAPER = Path(__file__).resolve().parents[1]
OUT = PAPER / 'pdf'
PAGE_W, PAGE_H = A4
MARGIN = 53
WIDTH = PAGE_W - 2 * MARGIN
INK = colors.HexColor('#20272D')
MUTED = colors.HexColor('#5A6872')
ACCENT = colors.HexColor('#286078')
RULE = colors.HexColor('#BCC7CD')
MAIN_NAME = 'event-memory-paper.zh-CN.pdf'
SUPP_NAME = 'event-memory-supplementary-tables.zh-CN.pdf'


def clean(text):
    return text.replace('\u2011', '-').replace('\u2013', '-').replace('\u2014', '-')


def escape(text):
    return html.escape(clean(text), quote=False)


def prose(text):
    # Use proportional Latin type alongside embedded Song-style Chinese text.
    # SimSun's Latin glyphs otherwise make bibliographic entries look spaced out.
    pieces = re.split(r'([\x20-\x7e\u00c0-\u024f]+)', clean(text))
    return ''.join(f'<font name="Latin">{escape(part)}</font>' if i % 2 else escape(part)
                   for i, part in enumerate(pieces) if part)


def inline(text):
    """Retain prose, emphasis and link labels; rewrite local PDF-relative links."""
    pattern = r'\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*'
    parts, start = [], 0
    for match in re.finditer(pattern, text):
        parts.append(prose(text[start:match.start()]))
        label, target, code, bold = match.groups()
        if target is not None:
            if target.startswith('supplementary-tables.md'):
                anchor = re.search(r'#表-s(\d+)', unquote(target), re.IGNORECASE)
                target = SUPP_NAME + ('#S' + anchor.group(1) if anchor else '')
            elif not re.match(r'https?://', target):
                # Preserve local companion material references without inventing
                # a public URL. Relative paths are based on paper/pdf/.
                if re.match(r'[A-Za-z]:[/\\]', target):
                    target = Path(target).as_uri()
                else:
                    target = '../' + target
                target = quote(unquote(target), safe=':/#?=&%')
            parts.append(f'<a href="{html.escape(target, quote=True)}" color="#286078">{prose(label)}</a>')
        elif code is not None:
            parts.append(f'<font name="Sans" size="9.2">{escape(code)}</font>')
        else:
            parts.append(f'<b>{prose(bold)}</b>')
        start = match.end()
    parts.append(prose(text[start:]))
    return ''.join(parts)


def make_styles():
    base = dict(fontName='Body', fontSize=10.5, leading=18.3,
                textColor=INK, wordWrap='CJK', spaceAfter=7,
                allowWidows=0, allowOrphans=0)
    body = ParagraphStyle('Body', **base)
    return {
        'body': body,
        'title': ParagraphStyle('Title', parent=body, fontName='Sans',
                                fontSize=23, leading=34, spaceAfter=13),
        'subtitle': ParagraphStyle('Subtitle', parent=body, fontName='Sans',
                                   fontSize=15.5, leading=25, spaceAfter=20),
        'meta': ParagraphStyle('Meta', parent=body, fontName='Sans',
                              fontSize=9, leading=15, textColor=MUTED, spaceAfter=12),
        'h1': ParagraphStyle('H1', parent=body, fontName='Sans', fontSize=15,
                            leading=23, spaceBefore=17, spaceAfter=10, keepWithNext=True),
        'h2': ParagraphStyle('H2', parent=body, fontName='Sans', fontSize=11.5,
                            leading=19, spaceBefore=11, spaceAfter=6, keepWithNext=True),
        'caption': ParagraphStyle('Caption', parent=body, fontSize=9,
                                 leading=14, spaceBefore=7, spaceAfter=11),
        'cell': ParagraphStyle('Cell', parent=body, fontSize=8.8,
                              leading=13.4, spaceAfter=0),
        'headcell': ParagraphStyle('HeadCell', parent=body, fontName='Sans',
                                  fontSize=9, leading=14, spaceAfter=0),
        'ref': ParagraphStyle('Reference', parent=body, fontSize=9.3, leading=15.5,
                             leftIndent=19, firstLineIndent=-19, spaceAfter=9),
    }


class VectorFigure(Flowable):
    def __init__(self, path, width=WIDTH):
        super().__init__()
        self.path = path
        box = PdfReader(path).pages[0].mediabox
        self.width = width
        self.height = width * float(box.height) / float(box.width)
        self.hAlign = 'CENTER'

    def draw(self):
        x, y = self.canv.absolutePosition(0, 0)
        self.canv._doctemplate.figure_placements.append({
            'path': str(self.path), 'page': self.canv.getPageNumber(),
            'x': x, 'y': y, 'width': self.width, 'height': self.height,
        })


class ReadingDoc(BaseDocTemplate):
    def __init__(self, filename, label, title, **kwargs):
        super().__init__(filename, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=52, bottomMargin=51, title=title, author='', **kwargs)
        self.label = label
        self.figure_placements = []
        self.heading_pages = []
        self.addPageTemplates(PageTemplate('Paper', [Frame(
            MARGIN, 51, WIDTH, PAGE_H - 103, leftPadding=0, rightPadding=0,
            topPadding=0, bottomPadding=0)], onPage=self.decorations))

    def beforeDocument(self):
        self.figure_placements = []
        self.heading_pages = []

    def decorations(self, canvas, doc):
        canvas.saveState()
        canvas.setFillColor(MUTED)
        canvas.setFont('Sans', 8)
        if doc.page > 1:
            canvas.drawString(MARGIN, PAGE_H - 31, '从交错对话到可追溯 Event')
            canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 31, self.label)
            canvas.setStrokeColor(RULE)
            canvas.setLineWidth(.4)
            canvas.line(MARGIN, PAGE_H - 39, PAGE_W - MARGIN, PAGE_H - 39)
        canvas.setFont('Sans', 8)
        canvas.drawString(MARGIN, 29, '中文研究稿 v0.19  |  2026-09-27')
        canvas.drawRightString(PAGE_W - MARGIN, 29, str(doc.page))
        canvas.restoreState()

    def afterFlowable(self, flowable):
        if hasattr(flowable, 'heading_key'):
            label = flowable.getPlainText()
            key, level = flowable.heading_key, flowable.heading_level
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(label, key, level=level, closed=False)
            self.heading_pages.append({'heading': label, 'page': self.page})
            if flowable.include_toc:
                self.notify('TOCEntry', (level, escape(label), self.page, key))


def heading(text, level, styles, key, toc=True):
    p = Paragraph(inline(text), styles['h1' if level == 0 else 'h2'])
    p.heading_key, p.heading_level, p.include_toc = key, level, toc
    return p


def markdown_blocks(text):
    return re.split(r'\n\s*\n', text.strip())


def markdown_table(block, styles):
    rows = [[cell.strip() for cell in row.strip().strip('|').split('|')]
            for row in block.splitlines()]
    rows.pop(1)
    ncols = len(rows[0])
    if ncols == 4:
        ratios = [.095, .285, .32, .30]
    elif ncols == 2:
        ratios = [.19, .81]
    elif rows[0][0] in ('维度', '对象'):
        ratios = [.17, .41, .42]
    elif rows[0][0] == '时间与来源':
        ratios = [.18, .38, .44]
    else:
        ratios = [.37, .31, .32]
    data = [[Paragraph(inline(cell), styles['headcell' if i == 0 else 'cell'])
             for cell in row] for i, row in enumerate(rows)]
    table = Table(data, colWidths=[WIDTH * r for r in ratios], repeatRows=1,
                  hAlign='LEFT', splitByRow=1)
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LINEABOVE', (0, 0), (-1, 0), .9, INK),
        ('LINEBELOW', (0, 0), (-1, 0), .6, INK),
        ('LINEBELOW', (0, -1), (-1, -1), .8, INK),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F5F7F8')]),
    ]))
    table.spaceAfter = 13
    return table


def toc(styles):
    result = TableOfContents()
    result.tableStyle = TableStyle([
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ])
    result.levelStyles = [
        ParagraphStyle('TOC0', fontName='Sans', fontSize=10, leading=16,
                       spaceBefore=5, textColor=INK),
        ParagraphStyle('TOC1', fontName='Body', fontSize=9, leading=14,
                       leftIndent=15, textColor=INK),
    ]
    result.dotsMinLevel = 0
    return result


def assemble(source, supplementary=False):
    styles = make_styles()
    blocks = markdown_blocks(source.read_text(encoding='utf-8'))
    title = blocks.pop(0).removeprefix('# ')
    metadata = blocks.pop(0)
    story = []
    if supplementary:
        story.extend([Paragraph('图表补充', styles['title']),
                      Paragraph('定义、案例与完整执行记录', styles['subtitle']),
                      Paragraph(inline(metadata), styles['meta']),
                      Paragraph(f'配套正文：<a href="{MAIN_NAME}" color="#286078">从交错对话到可追溯 Event</a>', styles['meta'])])
    else:
        primary, subtitle = title.split('：', 1)
        story.extend([Spacer(1, 30), Paragraph(escape(primary), styles['title']),
                      Paragraph(escape(subtitle), styles['subtitle']),
                      Paragraph('ChiYouyu · Haven', styles['body']),
                      Paragraph('中文稿 v0.19 · 仓库阅读版 · 2026-09-27', styles['meta'])])
        material_note = blocks.pop(0).removeprefix('> ').removeprefix('ChiYouyu · Haven。')
        story.append(Paragraph(inline(material_note), styles['meta']))
    i, number, references = 0, 0, False
    while i < len(blocks):
        block = blocks[i]
        if block.startswith('## '):
            text = block[3:]
            if not supplementary and text == '1. 引言':
                story.extend([PageBreak(), Paragraph('目录', styles['h1']), toc(styles), PageBreak()])
            if text == '参考文献':
                references = True
                story.append(PageBreak())
            h = heading(text, 0, styles, f'h{number}', toc=text != '摘要')
            if supplementary and i + 1 < len(blocks) and blocks[i + 1].startswith('|'):
                story.append(KeepTogether([h, markdown_table(blocks[i + 1], styles)]))
                i += 1
            else:
                story.append(h)
            number += 1
        elif block.startswith('### '):
            story.append(heading(block[4:], 1, styles, f'h{number}'))
            number += 1
        elif block.startswith('!['):
            match = re.fullmatch(r'!\[([^\]]+)\]\(([^)]+)\)', block)
            path = OUT / (Path(match.group(2)).stem + '.pdf')
            figure = VectorFigure(path, width=420 if 'figure-2' in path.name else WIDTH)
            caption = Paragraph(inline(blocks[i + 1]), styles['caption'])
            story.append(KeepTogether([Spacer(1, 8), figure, caption]))
            i += 1
        elif block.startswith('**表 ') and i + 1 < len(blocks) and blocks[i + 1].startswith('|'):
            caption = Paragraph(inline(block), styles['caption'])
            caption.keepWithNext = True
            table = markdown_table(blocks[i + 1], styles)
            story.extend([caption, table])
            i += 1
        elif block.startswith('|'):
            story.append(markdown_table(block, styles))
        elif references and re.match(r'^\d+\. ', block):
            for ref in block.splitlines():
                story.append(Paragraph(inline(ref), styles['ref']))
        elif block.startswith(('```', '- ', '* ')):
            raise ValueError('Unsupported Markdown block; add an explicit renderer: ' + block[:80])
        else:
            story.append(Paragraph(inline(block.replace('\n', ' ')), styles['body']))
        i += 1
    if not supplementary:
        story.extend([Spacer(1, 8), Paragraph(
            f'阅读版说明：扩展表格 S1-S5 见同目录的 <a href="{SUPP_NAME}" color="#286078">图表补充 PDF</a>。'
            '本目录仅随附正文、两图与补充表格，其余研究档案未随附。', styles['meta'])])
    return story, title


def build(source, output, supplementary=False):
    story, title = assemble(source, supplementary)
    with tempfile.TemporaryDirectory(prefix='paper-pdf-') as scratch:
        intermediate = Path(scratch) / 'layout.pdf'
        doc = ReadingDoc(str(intermediate), '图表补充' if supplementary else '系统案例研究', title)
        doc.multiBuild(story)
        writer = PdfWriter(clone_from=intermediate)
        for item in doc.figure_placements:
            figure = PdfReader(item['path']).pages[0]
            scale = item['width'] / float(figure.mediabox.width)
            writer.pages[item['page'] - 1].merge_transformed_page(
                figure, Transformation().scale(scale).translate(item['x'], item['y']))
        for entry in doc.heading_pages:
            match = re.match(r'表 S(\d+)\.', entry['heading'])
            if match:
                writer.add_named_destination('S' + match.group(1), entry['page'] - 1)
        # Use native PDF file navigation for the two companion PDFs. Generic
        # local Markdown material references remain URI links to the source tree.
        for page in writer.pages:
            for reference in page.get('/Annots', []):
                annotation = reference.get_object()
                action = annotation.get('/A')
                if not action:
                    continue
                uri = str(action.get('/URI', ''))
                filename, _, destination = uri.partition('#')
                if filename in (MAIN_NAME, SUPP_NAME):
                    annotation[NameObject('/A')] = DictionaryObject({
                        NameObject('/S'): NameObject('/GoToR'),
                        NameObject('/F'): TextStringObject(filename),
                        NameObject('/D'): TextStringObject(destination) if destination else
                            ArrayObject([NumberObject(0), NameObject('/Fit')]),
                    })
        writer.add_metadata({'/Title': title, '/Subject': '中文研究稿 v0.19，PDF 阅读版',
                             '/Creator': 'Markdown / ReportLab / pypdf', '/Author': 'ChiYouyu and Haven'})
        writer.write(output)
    return {'source': str(source.relative_to(PAPER)), 'output': output.name,
            'pages': len(writer.pages), 'headings': doc.heading_pages,
            'figures': doc.figure_placements}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--body-font', default=r'C:\Windows\Fonts\simsun.ttc')
    parser.add_argument('--heading-font', default=r'C:\Windows\Fonts\simhei.ttf')
    parser.add_argument('--latin-font', default=r'C:\Windows\Fonts\times.ttf')
    args = parser.parse_args()
    pdfmetrics.registerFont(TTFont('Body', args.body_font, subfontIndex=0))
    pdfmetrics.registerFont(TTFont('Sans', args.heading_font))
    pdfmetrics.registerFont(TTFont('Latin', args.latin_font))
    pdfmetrics.registerFontFamily('Body', normal='Body', bold='Sans', italic='Body', boldItalic='Sans')
    pdfmetrics.registerFontFamily('Sans', normal='Sans', bold='Sans', italic='Sans', boldItalic='Sans')
    pdfmetrics.registerFontFamily('Latin', normal='Latin', bold='Latin', italic='Latin', boldItalic='Latin')
    OUT.mkdir(parents=True, exist_ok=True)
    # Use the selected heading font for the SVG's Chinese labels. The browser
    # font subset embedded in the SVG is independent of ReportLab registration.
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPDF
    svg = (PAPER / 'figures/figure-1-system-flow.svg').read_text(encoding='utf-8')
    svg = re.sub(r'<style>.*?</style>', '', svg, flags=re.S)
    svg = svg.replace('font-family="PaperSans"', 'font-family="Sans"')
    drawing = svg2rlg(BytesIO(svg.encode('utf-8')))
    def set_figure_font(node):
        if hasattr(node, 'fontName'):
            node.fontName = 'Sans'
        for child in getattr(node, 'contents', []):
            set_figure_font(child)
    set_figure_font(drawing)
    renderPDF.drawToFile(drawing, str(OUT / 'figure-1-system-flow.pdf'))
    outputs = [build(PAPER / 'manuscript.zh-CN.md', OUT / MAIN_NAME),
               build(PAPER / 'supplementary-tables.md', OUT / SUPP_NAME, supplementary=True)]
    (OUT / 'reading-edition-build.json').write_text(
        json.dumps(outputs, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps([{'output': o['output'], 'pages': o['pages']} for o in outputs], ensure_ascii=False))


if __name__ == '__main__':
    main()
