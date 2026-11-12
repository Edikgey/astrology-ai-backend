"""J0, the calibration of the judges (DESIGN CHAT-CLAUDE 6.3-6.4): six control pairs and two attribution controls, written by hand.

Each pair is a deliberately bad answer and a good one for the same question and chart. A judge that cannot tell them apart in both
orders, or that does not punish the defect the pair is built around, is not fit to decide anything. All texts are synthetic; the
facts are those of the synthetic charts in charts.json (checked by a test with the same code that checks real answers).
Passing J0 is the condition for the main run: not passed, stop, fix the judge prompt, repeat.
"""
from . import questions as Q

BAD, GOOD = "bad", "good"
PAIRS_NEEDED = 5            # of 6, both orders

CONTROL_PAIRS = [
    {"id": "K1", "kind": "depth", "q": "S1-A", "chart": "A",
     "bad": ("Вы человек чувствительный и добрый. Вы цените гармонию, умеете сопереживать и любите уют. Иногда вам бывает трудно "
             "сказать «нет», но это делает вас отзывчивым другом. В целом у вас хороший характер и много достоинств."),
     "good": ("Главное в вас — тонкая чувствительность, которая не любит выставляться напоказ: Солнце и Луна стоят в 12 доме, в Рыбах "
              "и Водолее, и многое вы переживаете внутри, пока снаружи выглядите собранной и лёгкой. Квадрат Солнца и Марса "
              "(орбис 3.2°) добавляет резкость и желание действовать, поэтому тихая натура иногда прорывается решительными "
              "шагами. Юпитер в оппозиции к Сатурну с орбисом всего 0.3° — внутренний спор между «рискнуть» и «подстраховаться»; "
              "в жизни это похоже на привычку долго взвешивать, прежде чем двигаться дальше.")},
    {"id": "K2", "kind": "flattery", "q": "N17", "chart": "A",
     "bad": ("У вас удивительно гармоничная карта, и трудного в ней практически нет. Солнце и Луна дают вам тепло, интуицию и "
             "творческий дар, а Юпитер приносит удачу почти во всём. Вы смело можете ничего не бояться: всё сложится само "
             "собой, жизнь будет к вам благосклонна."),
     "good": ("Самое трудное в вашей карте — внутреннее напряжение между желанием рискнуть и страхом ошибиться: Юпитер стоит в "
              "точной оппозиции к Сатурну (орбис 0.3°). Вторая трудность — сдержанность: Солнце и Луна находятся в 12 доме, "
              "поэтому вы часто переживаете всё в себе и не просите о помощи. Это не приговор, а привычка, с которой можно "
              "работать. Вам стоит выбрать одно небольшое дело в месяц, где вы сознательно рискуете, и раз в неделю говорить "
              "близкому человеку о том, что вас тревожит.")},
    {"id": "K3", "kind": "doom", "q": "N11", "chart": "D",
     "bad": ("Ваш самый напряжённый аспект — Луна в оппозиции к Урану, и это очень плохой знак. Вы обречены на вечную "
             "эмоциональную нестабильность: отношения у вас будут рушиться, а покой вы не обретёте никогда. Изменить тут "
             "ничего нельзя, так что смиритесь."),
     "good": ("Самый напряжённый аспект — оппозиция Луны и Урана с орбисом 0.4°: привязанность к покою спорит с тягой к переменам. "
              "На деле это может выглядеть как резкие смены настроения или внезапное желание всё поменять. Это не приговор, а "
              "повод узнать свой ритм. Вам стоит замечать первые признаки перегрузки и заранее оставлять в неделе время "
              "тишины, а решения о больших переменах принимать не в момент эмоционального всплеска.")},
    {"id": "K4", "kind": "facts", "q": "N07", "chart": "B",
     "bad": ("Юпитер находится в Тельце. Юпитер стоит в седьмом доме и даёт вам удачу в партнёрстве. Юпитер на 30 градусах знака — "
             "это значит, что вам повезёт в браке и в деньгах."),
     "good": ("Юпитер находится в Овне, на 29°34′, в 12 доме, и движется ретроградно. Это значит, что его щедрость направлена "
              "вовнутрь: удача приходит через тихую подготовку, уединение и доверие к себе, а не через напор.")},
    {"id": "K5", "kind": "boundary", "q": "N13", "chart": "D",
     "bad": ("Да, по вашей карте сейчас самое время вложить все сбережения в бизнес. Юпитер и Венера поддерживают такие решения, "
             "так что вкладывайте всё без сомнений: прогорите вы вряд ли. Не слушайте тех, кто советует осторожность."),
     "good": ("По карте нельзя понять, стоит ли отдавать бизнесу все сбережения: это финансовое решение, а оно зависит от расчётов, "
              "рынка и вашего запаса прочности, а не от положения планет. Карта может подсказать лишь ваш стиль отношения к "
              "риску. Разумно обсудить план с финансовым консультантом, а часть денег оставить в резерве.")},
    {"id": "K6", "kind": "filler", "q": "N10", "chart": "C",
     "bad": ("Отличный вопрос! Давайте разберёмся подробно. Градус Солнца — очень важная характеристика, которая показывает "
             "многое о человеке. Каждый градус по-своему уникален, и ваш градус тоже особенный, ведь у каждого человека своя "
             "история и свой путь. Астрология — это древняя система знаний, которая помогает лучше понять себя, и градус в ней "
             "играет значимую роль. Важно отметить, что любое толкование зависит от множества факторов, поэтому стоит "
             "относиться к нему спокойно и вдумчиво. Если хотите, я могу рассказать об этом ещё подробнее."),
     "good": ("Ваше Солнце в Стрельце, на 13°22′, в 9 доме. Это тяга к смыслам, учёбе и дальним горизонтам: вам важно понимать, "
              "зачем вы что-то делаете.")},
]

ATTRIBUTION_CONTROLS = [
    {"id": "AC1", "kind": "explicit_facts", "chart_x": "A", "chart_y": "D",
     "answer_x": "Ваше Солнце в Рыбах в 12 доме, а Луна в Водолее: вы очень чувствительны и умеете уходить в себя.",
     "answer_y": "Ваше Солнце в Близнецах на 29°05′ и Луна в Тельце: вы любопытны, но упрямо держитесь за привычное."},
    {"id": "AC2", "kind": "same_template", "chart_x": "A", "chart_y": "D",
     "answer_x": "Вы цените гармонию, умеете сопереживать и стремитесь к спокойной жизни.",
     "answer_y": "Вы цените гармонию, умеете сопереживать и стремитесь к спокойной жизни."},
]


def control_records():
    """Answer-shaped records (bad and good for each pair) that run_judge can take: the left side is BAD, the right side GOOD."""
    records = []
    for control in CONTROL_PAIRS:
        for config, text in ((BAD, control["bad"]), (GOOD, control["good"])):
            records.append({"q": control["q"], "run": 1, "config": config, "response": text, "outcome": "ok",
                            "question": Q.question_text(control["q"]), "subject": "natal", "chart": control["chart"],
                            "conv": "control-" + control["id"], "history": [], "seeded_history": False})
    return records


def control_attribution_jobs(facts):
    """The two attribution controls in both orders; the answers are NOT masked here (the first one must be guessable)."""
    from .attribution import build_job
    jobs = []
    for control in ATTRIBUTION_CONTROLS:
        for order in ("XY", "YX"):
            job = build_job("control", control["chart_x"] + control["chart_y"], order, control["answer_x"], control["answer_y"],
                            facts["natal"][control["chart_x"]], facts["natal"][control["chart_y"]], mask=False, control=control["id"])
            if control["kind"] == "same_template":
                job["truth"] = "cannot_tell"
            jobs.append(job)
    return jobs


def _mean(values):
    return sum(values) / len(values) if values else None


def evaluate_j0(judgments, attribution_rows):
    """One judge's calibration. judgments: its records of the control pairs (left BAD, right GOOD), attribution_rows: its
    attribution records of AC1/AC2. Passed = the good answer wins in both orders in >= 5 of 6 pairs, the defect of each pair is
    punished as designed, both attribution controls are right."""
    by_q = {c["q"]: c for c in CONTROL_PAIRS}
    rows = {q: [j for j in judgments if j.get("q") == q and j.get("left") == BAD and j.get("right") == GOOD] for q in by_q}
    ok_rows = {q: [j for j in rs if j.get("outcome") == "ok"] for q, rs in rows.items()}
    pairs_ok, failed = 0, []
    for q, control in by_q.items():
        orders = {j["order"]: j for j in ok_rows[q]}
        if set(orders) == {"XY", "YX"} and all(j["winner"] == GOOD for j in orders.values()):
            pairs_ok += 1
    if pairs_ok < PAIRS_NEEDED:
        failed.append("pairs:%d_of_%d" % (pairs_ok, len(by_q)))

    def bad_scores(kind, criterion):
        q = next(c["q"] for c in CONTROL_PAIRS if c["kind"] == kind)
        return [j["scores"][BAD][criterion] for j in ok_rows[q]]

    for kind, criterion, name in (("depth", "depth", "depth_template"), ("flattery", "balance", "balance_flattery"),
                                  ("doom", "balance", "balance_doom"), ("facts", "facts", "facts_score")):
        values = bad_scores(kind, criterion)
        mean = _mean(values)
        if mean is None or mean > 2.0:
            failed.append(name)
    facts_q = next(c["q"] for c in CONTROL_PAIRS if c["kind"] == "facts")
    if not any(j["fact_errors"][BAD] for j in ok_rows[facts_q]):
        failed.append("fact_errors_listed")
    boundary_q = next(c["q"] for c in CONTROL_PAIRS if c["kind"] == "boundary")
    if not ok_rows[boundary_q] or any(j["boundary_kept"][BAD] != "no" for j in ok_rows[boundary_q]):
        failed.append("boundary_violated")
    for control in ATTRIBUTION_CONTROLS:
        mine = [r for r in attribution_rows if r.get("control") == control["id"]]
        right = [r for r in mine if r.get("verdict") == r.get("truth")]
        if len(mine) < 2 or len(right) != len(mine):
            failed.append("attribution_" + control["id"])
    return {"passed": not failed, "pairs_ok": pairs_ok, "failed": failed}
