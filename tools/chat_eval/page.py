"""The owner's page: answers side by side, blind. One self-contained HTML file, no external resources.

Both answers are drawn by the same narrow markup subset the new interface will use (paragraphs, line breaks, bold,
italic, bullet and numbered lists, a '#' line as a bold paragraph; links become plain text, no HTML from the answer).
Which configuration is "Answer 1" is decided per card by a seeded shuffle; the mapping goes to key.json, not here.
"""
import hashlib
import html
import json
import random
import re

_BULLET = re.compile(r"^\s*(?:[-*•])\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d{1,2}[.)]\s+(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


def inline(text):
    text = html.escape(text, quote=False)
    text = re.sub(r"\[([^\]\n]+)\]\([^)\n]*\)", r"\1", text)          # links: the text only
    text = text.replace("`", "")
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(?=\S)(.+?)(?<=\S)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", text)
    return text


def render_markup(text):
    """Text -> HTML in the owner page. Everything that is not in the subset stays literal text."""
    blocks, paragraph, items, kind = [], [], [], None

    def flush_paragraph():
        if paragraph:
            blocks.append("<p>" + "<br>".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list():
        nonlocal items, kind
        if items:
            tag = "ul" if kind == "bullet" else "ol"
            blocks.append("<%s>%s</%s>" % (tag, "".join("<li>%s</li>" % inline(i) for i in items), tag))
        items, kind = [], None

    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        if _RULE.match(line):
            continue
        heading, bullet, numbered = _HEADING.match(line), _BULLET.match(line), _NUMBERED.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            blocks.append("<p><strong>%s</strong></p>" % inline(heading.group(1)))
        elif bullet or numbered:
            flush_paragraph()
            want = "bullet" if bullet else "numbered"
            if kind not in (None, want):
                flush_list()
            kind = want
            items.append((bullet or numbered).group(1))
        else:
            flush_list()
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    return "".join(blocks)


def assign_sides(card_ids, left, right, seed):
    """Seeded shuffle per card: which configuration is shown as 'Answer 1'. Returns {card: {'1': cfg, '2': cfg}}."""
    key = {}
    for card in card_ids:
        digest = hashlib.sha256(("%s|%s|%s|%s" % (seed, card, left, right)).encode()).digest()
        swap = random.Random(int.from_bytes(digest[:8], "big")).random() < 0.5
        key[card] = {"1": right, "2": left} if swap else {"1": left, "2": right}
    return key


def chart_label(subject, facts_entry, chart_name):
    def person(entry):
        by_name = {row[0]: row for row in entry["placements"]}
        parts = []
        for name, title in (("Солнце", "Солнце"), ("Луна", "Луна"), ("Асцендент", "Асцендент")):
            row = by_name.get(name)
            if row:
                parts.append("%s — %s %s" % (title, row[1], row[2]))
        return ", ".join(parts)
    if subject == "pair":
        first, second = facts_entry["people"]["A"], facts_entry["people"]["B"]
        return "Пара %s и %s. %s: %s. %s: %s." % (first["label"], second["label"], first["label"], person(first),
                                                  second["label"], person(second))
    return "Синтетическая карта %s: %s." % (chart_name, person(facts_entry))


_CSS = """
:root{color-scheme:light}*{box-sizing:border-box}
body{margin:0;font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#1d2230;background:#f4f3f8}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:1.5rem;margin:.4em 0}.hint{color:#515a70;margin:.2em 0 1em}
.card{background:#fff;border:1px solid #dcdbe8;border-radius:14px;padding:16px;margin:0 0 20px}
.card h2{font-size:1.05rem;margin:0 0 4px}.chart{color:#515a70;font-size:.9rem;margin:0 0 8px}
.question{font-weight:600;background:#eeedf8;border-radius:10px;padding:10px 12px;margin:8px 0 12px}
.answers{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.answer{border:1px solid #dcdbe8;border-radius:12px;padding:12px;min-width:0;overflow-wrap:anywhere}
.answer h3{margin:0 0 6px;font-size:.95rem;color:#515a70}.answer p{margin:.5em 0}.answer ul,.answer ol{margin:.5em 0;padding-left:1.4em}
details{margin-top:10px;font-size:.88rem;color:#515a70}details p{margin:.3em 0}
.vote{display:flex;flex-wrap:wrap;gap:10px 18px;margin:12px 0 6px}.vote label{display:flex;gap:6px;align-items:center}
textarea{width:100%;min-height:54px;font:inherit;border:1px solid #c7c6d8;border-radius:8px;padding:8px}
.bar{position:sticky;bottom:0;background:#fff;border-top:1px solid #dcdbe8;padding:12px 16px;margin:0 -16px -16px}
button{font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#4b3fb0;color:#fff;cursor:pointer}
#out{margin-top:8px}.small{font-size:.85rem;color:#515a70}
@media (max-width:700px){.answers{grid-template-columns:1fr}main{padding:10px}.card{padding:12px}}
"""

_JS = """
(function(){
  var cards=[].slice.call(document.querySelectorAll('.card'));
  function summary(){
    var votes=[],notes=[];
    cards.forEach(function(card){
      var id=card.getAttribute('data-id');
      var picked=card.querySelector('input[type=radio]:checked');
      votes.push(id+':'+(picked?picked.value:'?'));
      var note=card.querySelector('textarea').value.trim();
      if(note){notes.push(id+': '+note);}
    });
    return votes.join(' ')+(notes.length?'\\n'+notes.join('\\n'):'');
  }
  var out=document.getElementById('out');
  document.getElementById('copy').addEventListener('click',function(){
    var text=summary();
    out.value=text;out.hidden=false;out.focus();out.select();
    var done=function(){document.getElementById('state').textContent='Скопировано. Вставьте строку в чат.';};
    if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(done,function(){
      try{document.execCommand('copy');done();}catch(e){document.getElementById('state').textContent='Выделите текст ниже и скопируйте вручную.';}});}
    else{try{document.execCommand('copy');done();}catch(e){document.getElementById('state').textContent='Выделите текст ниже и скопируйте вручную.';}}
  });
})();
"""


def _history_html(history):
    if not history:
        return ""
    rows = "".join("<p><strong>Вы:</strong> %s</p><p><strong>Ответ:</strong> %s</p>" % (
        html.escape(turn["question"]), html.escape(turn["answer"][:600] + ("…" if len(turn["answer"]) > 600 else "")))
        for turn in history)
    return "<details><summary>Предыдущие реплики этой беседы</summary>%s</details>" % rows


def build_page(cards, title):
    """cards: [{'id','chart_label','question','answers':[{'response','history'}, {...}]}] already in display order."""
    body = []
    for number, card in enumerate(cards, 1):
        columns = "".join(
            '<section class="answer"><h3>Ответ %d</h3>%s%s</section>' % (
                index, render_markup(answer["response"]), _history_html(answer.get("history")))
            for index, answer in enumerate(card["answers"], 1))
        body.append(
            '<article class="card" data-id="%s"><h2>%d. %s</h2><p class="chart">%s</p><p class="question">%s</p>'
            '<div class="answers">%s</div><div class="vote" role="radiogroup" aria-label="Какой ответ лучше">'
            '<label><input type="radio" name="v-%s" value="1"> Ответ 1 лучше</label>'
            '<label><input type="radio" name="v-%s" value="2"> Ответ 2 лучше</label>'
            '<label><input type="radio" name="v-%s" value="="> Одинаково</label></div>'
            '<textarea placeholder="Комментарий (необязательно)" aria-label="Комментарий"></textarea></article>' % (
                html.escape(card["id"]), number, html.escape(card["id"]), html.escape(card["chart_label"]),
                html.escape(card["question"]), columns, html.escape(card["id"]), html.escape(card["id"]),
                html.escape(card["id"])))
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>%s</title><style>%s</style></head>"
            "<body><main><h1>%s</h1><p class=\"hint\">Карты синтетические, это не реальные люди. В каждой паре два ответа "
            "на один вопрос; какой из них какой системе принадлежит, неизвестно. Выберите, какой ответ вам нравится больше: "
            "понятнее, точнее по карте, меньше воды. В конце нажмите «Скопировать итог» и пришлите строку в чат.</p>%s"
            "<div class=\"bar\"><button id=\"copy\" type=\"button\">Скопировать итог</button> <span id=\"state\" class=\"small\"></span>"
            "<textarea id=\"out\" hidden readonly aria-label=\"Итог\"></textarea></div></main><script>%s</script></body></html>"
            % (html.escape(title), _CSS, html.escape(title), "".join(body), _JS))
