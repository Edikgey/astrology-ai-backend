"""Compact projection of saved A/B facts; no astronomical or synastry recalculation."""
from copy import deepcopy
from fastapi import HTTPException
from database.queries import NatalChart, ChartData
from modules.ai_subject import owned_subject
from modules.ai_chart_context import compact_json
from modules.synastry import PLANETS, ANGLES, MAJOR_ASPECTS, validate_synastry_input, SynastryInputError

RELATIONSHIP_CONTEXT_BUDGET = 32000  # UTF-8 bytes, within the shared 64000-byte total guard.


def build_relationship_context(db, user_id, relationship_id):
    relation = owned_subject(db, user_id, relationship_id=relationship_id)
    snapshot = relation.calculation
    try:
        if snapshot['schema_version'] != 1 or snapshot['ruleset_version'] != relation.ruleset_version:
            raise ValueError('unsupported snapshot')
        people = {}
        for participant, chart_id, label in (('A', relation.chart_a_id, relation.person_a_label),
                                            ('B', relation.chart_b_id, relation.person_b_label)):
            chart = db.query(NatalChart).filter_by(id=chart_id, user_id=user_id).one()
            rows = db.query(ChartData).filter_by(chart_id=chart_id).all()
            if len(rows) != 1:
                raise ValueError('ambiguous chart data')
            saved = rows[0]
            facts = validate_synastry_input(chart, saved, participant)
            if facts != snapshot['inputs'][participant]:
                raise ValueError('saved chart changed since snapshot')
            positions = {**facts['positions'], **facts['angles']}
            # Natal aspects are saved facts, not inferred from degrees. Drop excluded bodies/types.
            aspects = sorted({(min(a['from_body'], a['to_body']), max(a['from_body'], a['to_body']), a['aspect'])
                for a in saved.aspects_for_circle or [] if a['from_body'] in positions and a['to_body'] in positions
                and a['aspect'] in {symbol for symbol, _ in MAJOR_ASPECTS}})
            people[participant] = {'chart_id': chart_id, 'label': label,
                'placement_columns': ['body', 'longitude_deg', 'sign', 'natal_house', 'retrograde'],
                'placements': [[body, longitude, saved.bodies_for_circle[body].get('sign'),
                                saved.bodies_for_circle[body].get('house') if facts['cusps'] is not None else None,
                                saved.bodies_for_circle[body].get('retrograde')]
                               for body, longitude in positions.items()],
                'natal_aspect_columns': ['body_a', 'body_b', 'aspect'], 'natal_aspects': aspects,
                'house_system': facts['house_system'], 'cusps': facts['cusps']}
        aspects = deepcopy(snapshot['aspects'])
        # Ranking changes presentation/context selection only, never recomputes an aspect.
        personal = set(PLANETS[:5])
        aspects.sort(key=lambda a: (not (a['body_a'] in personal or a['body_b'] in personal),
                                   a['orb_deg'], a['participant_a'], a['body_a'], a['participant_b'], a['body_b'], a['aspect']))
        context = {'relationship_id': relation.id, 'speaker_person': relation.speaker_person,
            'ruleset_version': snapshot['ruleset_version'], 'schema_version': snapshot['schema_version'],
            'people': people, 'interchart_aspects': aspects,
            'angle_availability': snapshot['angle_availability'], 'house_overlays': snapshot['house_overlays'],
            'limitations': ['Saved natal/synastry data only; no transits or compatibility score.',
                             'Time provenance validates conversion, not the accuracy of reported birth time.',
                             'Unavailable angles/overlays must remain unknown.'],
            'omitted': {'natal_aspects': {'A': 0, 'B': 0}, 'interchart_aspects': 0}}
        # Retain participants, all core placements and availability. Remove only complete rows.
        def size():
            return len(compact_json(context).encode('utf-8'))
        for participant in ('B', 'A'):
            rows = people[participant]['natal_aspects']
            while rows and size() > RELATIONSHIP_CONTEXT_BUDGET:
                rows.pop()
                context['omitted']['natal_aspects'][participant] += 1
        while aspects and size() > RELATIONSHIP_CONTEXT_BUDGET:
            aspects.pop()
            context['omitted']['interchart_aspects'] += 1
        if size() > RELATIONSHIP_CONTEXT_BUDGET:
            raise HTTPException(422, 'Данные пары превышают бюджет AI-контекста; требуется проверка анализа.')
        return compact_json(context)
    except (KeyError, TypeError, ValueError, SynastryInputError):
        raise HTTPException(409, detail={'code': 'RELATIONSHIP_CONTEXT_UNAVAILABLE',
            'message': 'Сохранённые данные анализа неполны или изменились. Создайте актуальный анализ.'}) from None
