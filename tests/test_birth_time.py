"""Birth input/UTC/house regression tests; no OpenAI or geocoder network calls."""
import json
import unittest
from datetime import datetime
from unittest.mock import patch
import swisseph as swe
import test_my_charts as f
from models.natal_chart import NatalChartCreate
from database.queries import NatalChart, ChartData
from modules.birth_time import resolve_birth_time, calculator_args
from modules.ephemeris import Ephemeris, HOUSE_SYSTEM_CODE
from modules.ai_chart_context import build_chart_context


class BirthTimeTests(unittest.TestCase):
    def data(self, **overrides):
        return NatalChartCreate(**{**f.PAYLOAD, **overrides})

    def test_minutes_and_utc_day_month_year_rollover(self):
        local, utc = resolve_birth_time(self.data(year=2000, month=1, day=1, hour=0, minute=15, timezone='Asia/Kathmandu'))
        self.assertEqual(local, .25)
        self.assertEqual(utc, datetime(1999,12,31,18,30))
        _, utc = resolve_birth_time(self.data(year=2024, month=12, day=31, hour=23, minute=45, timezone='America/New_York'))
        self.assertEqual(utc, datetime(2025,1,1,4,45))

    def test_historical_dst_and_standard_time_use_birth_date_not_current_offset(self):
        _, summer = resolve_birth_time(self.data(year=2024,month=7,day=1,hour=12,minute=37,timezone='Europe/Berlin'))
        _, winter = resolve_birth_time(self.data(year=2024,month=1,day=1,hour=12,minute=37,timezone='Europe/Berlin'))
        self.assertEqual(summer,datetime(2024,7,1,10,37))
        self.assertEqual(winter,datetime(2024,1,1,11,37))

    def test_nonexistent_and_ambiguous_local_times_are_not_guessed(self):
        with self.assertRaises(Exception) as exc:
            resolve_birth_time(self.data(year=2024,month=3,day=10,hour=2,minute=30,timezone='America/New_York'))
        self.assertEqual(exc.exception.detail['code'],'NONEXISTENT_BIRTH_TIME')
        repeated=dict(year=2024,month=11,day=3,hour=1,minute=30,timezone='America/New_York')
        with self.assertRaises(Exception) as exc: resolve_birth_time(self.data(**repeated))
        self.assertEqual(exc.exception.detail['code'],'AMBIGUOUS_BIRTH_TIME')
        self.assertEqual(resolve_birth_time(self.data(**repeated,time_fold=0))[1],datetime(2024,11,3,5,30))
        self.assertEqual(resolve_birth_time(self.data(**repeated,time_fold=1))[1],datetime(2024,11,3,6,30))

    def test_missing_invalid_zone_date_and_conflicting_hour_are_rejected(self):
        for changes in ({'timezone':None},{'timezone':'Invalid/Zone'},{'year':2023,'month':2,'day':29},
                        {'hour':12.5,'minute':30}):
            with self.subTest(changes=changes), self.assertRaises(Exception) as exc:
                resolve_birth_time(self.data(**changes))
            self.assertEqual(exc.exception.status_code,422)

    def test_decimal_hour_clients_and_legacy_records_keep_minutes(self):
        self.assertEqual(resolve_birth_time(self.data(hour=12.75,timezone='UTC'))[1],datetime(2000,1,2,12,45))
        chart=NatalChart(**{**f.PAYLOAD,'timezone':None,'hour':12.75})
        self.assertEqual(calculator_args(chart),(2000,1,2,12.75,30.,50.))

    def test_house_cusps_use_same_placidus_system_and_full_precision(self):
        ephem=Ephemeris(2000,1,2,12.5,30,50)
        expected,_=swe.houses(ephem.jd,50,30,b'P')
        self.assertEqual(HOUSE_SYSTEM_CODE,b'P')
        self.assertEqual([r['degree'] for r in ephem.get_house_cusps()],list(expected))
        houses=ephem.get_houses()
        for index,cusp in enumerate(expected):
            self.assertEqual(ephem._find_house(cusp,'unused',houses,True),index+1)
            self.assertEqual(ephem._find_house(cusp+360,'unused',houses,True),index+1)
            previous=(index-1)%12+1
            self.assertEqual(ephem._find_house(cusp-1e-7,'unused',houses,True),previous)

    def test_planet_assignment_uses_actual_longitude_not_display_rounding(self):
        ephem=Ephemeris(2000,1,2,12,30,50)
        # The second cusp cuts through a displayed arcminute. Rounding to 15°00'
        # would place the planet in house 1; its real longitude belongs to house 2.
        houses={f'Дом {i+1}': (345.005+i*30)%360 for i in range(12)}
        with patch.object(ephem,'get_houses',return_value=houses), patch('modules.ephemeris.swe.calc_ut',return_value=((15.009,0,0,1,0,0),0)):
            sun=ephem.get_planets()['Солнце']
        self.assertEqual(sun['дом'],2)
        self.assertEqual(sun['абс_долгота'],15.009)

    def test_planet_houses_match_returned_cusp_intervals(self):
        ephem=Ephemeris(2000,1,2,12.5,30,50)
        houses=ephem.get_houses()
        for planet in ephem.get_planets().values():
            self.assertEqual(planet['дом'],ephem._find_house(planet['абс_долгота'],None,houses,True))

    def test_zero_longitude_is_retained_in_saved_placements(self):
        ephem=Ephemeris(2000,1,2,12,30,50)
        data={'Солнце':{'абс_долгота':0.0,'дом':1,'ретроградный':False}}
        with patch.object(ephem,'get_planets',return_value=data), patch.object(ephem,'get_asteroids',return_value={}), patch.object(ephem,'get_key_points',return_value={}):
            self.assertEqual(ephem.get_all_bodies_with_degrees()['☉']['degree'],0.0)

    def test_calculated_points_store_their_own_longitudes_even_if_jpl_is_unavailable(self):
        ephem=Ephemeris(2000,1,2,12,30,50)
        with patch('modules.ephemeris.Horizons',side_effect=RuntimeError('mock unavailable')), patch.object(ephem,'calculate_selena',return_value=240.), patch('modules.ephemeris.swe.calc_ut',return_value=((120.,0,0,1,0,0),0)):
            data=ephem.get_asteroids()
        self.assertEqual(data['Черная Луна Лилит']['абс_долгота'],120.)
        self.assertEqual(data['Белая Луна Селена']['абс_долгота'],240.)


class BirthPipelineTests(unittest.TestCase):
    setUp=f.MyChartsTests.setUp
    tearDown=f.MyChartsTests.tearDown
    chart=f.MyChartsTests.chart
    mock_calculations=f.MyChartsTests.mock_calculations

    def test_selected_location_snapshot_rejects_stale_fields_before_calculation(self):
        snapshot={key:f.PAYLOAD[key] for key in ('city','region','country','lat','lon','timezone')}
        for field, value in [('city','Changed city'),('region','Changed region'),('country','Changed country'),
                             ('lat',0),('lon',0),('timezone','Europe/Warsaw')]:
            with self.subTest(field=field), patch('api.endpoints.Ephemeris') as calculator:
                response=self.client.post('/natal-chart',json={**f.PAYLOAD,'selected_location':snapshot,field:value},headers=self.owner)
                self.assertEqual(response.status_code,422,response.text)
                calculator.assert_not_called()
        with self.sessions() as db:self.assertEqual(db.query(NatalChart).count(),0)

    def test_consistent_provider_selection_is_saved_without_geographic_guessing(self):
        payload={**f.PAYLOAD,'city':'Toronto, Ontario, Canada','region':'Ontario','country':'Canada',
                 'lat':43.65,'lon':-79.38,'timezone':'America/Toronto','hour':12,'minute':37}
        payload['selected_location']={key:payload[key] for key in ('city','region','country','lat','lon','timezone')}
        with self.mock_calculations():
            response=self.client.post('/natal-chart',json=payload,headers=self.owner)
        self.assertEqual(response.status_code,200,response.text)
        with self.sessions() as db:
            chart=db.get(NatalChart,response.json()['chart_id'])
            for field,value in payload['selected_location'].items():self.assertEqual(getattr(chart,field),value)
            self.assertEqual(chart.birth_utc,datetime(2000,1,2,17,37))

    def test_selected_location_with_invalid_zone_is_rejected_without_fallback(self):
        for zone in ('', 'Invalid/Zone'):
            payload={**f.PAYLOAD,'timezone':zone}
            payload['selected_location']={key:payload[key] for key in ('city','region','country','lat','lon','timezone')}
            with self.subTest(zone=zone), patch('api.endpoints.Ephemeris') as calculator:
                response=self.client.post('/natal-chart',json=payload,headers=self.owner)
                self.assertEqual(response.status_code,422,response.text)
                calculator.assert_not_called()

    def test_full_local_time_zone_utc_and_house_snapshot_survive_db_and_refresh(self):
        payload={**f.PAYLOAD,'year':2000,'month':1,'day':1,'hour':0,'minute':15,'timezone':'Asia/Kathmandu'}
        with self.mock_calculations(), patch('api.endpoints.Ephemeris',wraps=None) as ephem:
            ephem.return_value.get_all_bodies_with_degrees.return_value=f.BODY
            ephem.return_value.get_all_bodies_with_symbols.return_value={}
            ephem.return_value.get_all_bodies.return_value={}
            houses=[{'symbol':str(i),'degree':i*20.123456} for i in range(1,13)]
            ephem.return_value.get_house_cusps.return_value=houses
            with patch('api.endpoints.Aspects') as aspects:
                aspects.return_value.get_all_aspects_flat.return_value=''
                aspects.return_value.convert_aspects_for_chart.return_value=[]
                aspects.return_value.convert_aspects_to_symbols.return_value={}
                aspects.return_value.get_all_aspects_structured.return_value={}
                response=self.client.post('/natal-chart',json=payload,headers=self.owner)
                self.assertEqual(response.status_code,200,response.text)
                ephem.assert_called_once_with(1999,12,31,18.5,30.,50.)
                aspects.assert_called_once_with(1999,12,31,18.5,30.,50.)
        cid=response.json()['chart_id']
        with self.sessions() as db:
            chart=db.get(NatalChart,cid);data=db.query(ChartData).filter_by(chart_id=cid).one()
            self.assertEqual((chart.year,chart.month,chart.day,chart.hour),(2000,1,1,.25))
            self.assertEqual(chart.timezone,'Asia/Kathmandu')
            self.assertEqual(chart.birth_utc,datetime(1999,12,31,18,30))
            self.assertEqual(data.houses,houses);self.assertEqual(data.house_system,'Placidus')
            with patch('modules.ephemeris.Ephemeris',side_effect=AssertionError('AI must not calculate')):
                context=json.loads(build_chart_context(db,1,cid))
            self.assertEqual(context['houses']['cusp_longitudes_deg'],houses)
            self.assertEqual(context['birth']['utc'],'1999-12-31T18:30:00Z')
        with patch('api.endpoints.Ephemeris',side_effect=AssertionError('Saved houses must be used')):
            loaded=self.client.get(f'/natal-chart/{cid}',headers=self.owner)
            self.assertEqual(loaded.status_code,200,loaded.text)
            self.assertEqual(loaded.json()['houses'],houses)
            self.assertEqual(loaded.json()['time_provenance'],'local_iana')

    def test_legacy_without_zone_and_saved_cusps_remains_readable_without_ai_calculation(self):
        cid=self.chart()
        with self.sessions() as db:
            db.get(NatalChart,cid).timezone=None;db.commit()
            with patch('modules.ephemeris.swe.houses',side_effect=AssertionError('AI must only read stored data')):
                context=json.loads(build_chart_context(db,1,cid))
            self.assertIsNone(context['houses']['cusp_longitudes_deg'])
            self.assertIsNone(context['birth']['timezone'])
            self.assertIn('Legacy',context['limitations'])
        response=self.client.get(f'/natal-chart/{cid}',headers=self.owner)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['time_provenance'],'legacy_unverified')

    def test_bad_birth_time_is_rejected_before_calculator_or_db_write(self):
        with self.mock_calculations(), patch('api.endpoints.Ephemeris') as calculator:
            response=self.client.post('/natal-chart',json={**f.PAYLOAD,'timezone':None},headers=self.owner)
            self.assertEqual(response.status_code,422)
            calculator.assert_not_called()
        with self.sessions() as db:self.assertEqual(db.query(NatalChart).count(),0)
