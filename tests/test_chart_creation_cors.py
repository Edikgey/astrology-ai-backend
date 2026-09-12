"""Real Swiss Ephemeris -> API -> DB regressions; only JPL network is stubbed."""
import unittest
from unittest.mock import patch
import swisseph as swe
import test_my_charts as f
from database.queries import NatalChart, ChartData
from modules.ephemeris import Ephemeris

ORIGIN = 'https://astrology-ai-frontend-production.up.railway.app'


class ChartCreationCorsTests(unittest.TestCase):
    setUp = f.MyChartsTests.setUp
    tearDown = f.MyChartsTests.tearDown

    def test_preflight_accepts_production_jwt_and_guest_headers(self):
        for headers in ('content-type,authorization', 'content-type,x-session-token'):
            response=self.client.options('/natal-chart',headers={'Origin':ORIGIN,
                'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':headers})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers.get('access-control-allow-origin'),ORIGIN)

    def test_disallowed_origin_is_not_reflected(self):
        response=self.client.options('/natal-chart',headers={'Origin':'https://untrusted.example',
            'Access-Control-Request-Method':'POST'})
        self.assertEqual(response.status_code,400)
        self.assertNotIn('access-control-allow-origin',response.headers)

    def test_schema_error_has_cors_and_never_calculates(self):
        with patch('api.endpoints.Ephemeris') as calculator:
            response=self.client.post('/natal-chart',json={**f.PAYLOAD,'timezone':None},
                headers={**self.owner,'Origin':ORIGIN})
            self.assertEqual(response.status_code,422)
            self.assertEqual(response.headers.get('access-control-allow-origin'),ORIGIN)
            calculator.assert_not_called()

    def test_unhandled_calculator_error_keeps_500_and_cors_without_saving(self):
        with patch('api.endpoints.Ephemeris',side_effect=RuntimeError('simulated internal error')):
            response=self.client.post('/natal-chart',json=f.PAYLOAD,headers={**self.owner,'Origin':ORIGIN})
        self.assertEqual(response.status_code,500)
        self.assertEqual(response.headers.get('access-control-allow-origin'),ORIGIN)
        self.assertNotIn('simulated internal error',response.text)
        with self.sessions() as db:self.assertEqual(db.query(NatalChart).count(),0)

    def test_vertex_matches_native_swiss_output_across_hemispheres(self):
        for lat,lon in [(0,0),(.35,32.58),(-1.29,36.82),(-33.87,151.21),(51.5,-.12)]:
            with self.subTest(lat=lat,lon=lon):
                ephem=Ephemeris(2000,1,1,9.5,lon,lat)
                native=swe.houses(ephem.jd,lat,lon,b'P')[1][3]
                self.assertAlmostEqual(ephem.calculate_vertex(),native,places=10)
                self.assertAlmostEqual(ephem.get_key_points()['Вертекс']['абс_долгота'],native,places=10)
                self.assertAlmostEqual(ephem.get_key_point_longitudes()['Вертекс'],native,places=10)

    def test_real_chart_calculation_saves_equatorial_and_southern_inputs(self):
        for lat,lon,zone in [(.35,32.58,'Africa/Kampala'),(0,-78.5,'America/Guayaquil'),
                             (-33.87,151.21,'Australia/Sydney')]:
            with self.subTest(lat=lat,zone=zone):
                payload={**f.PAYLOAD,'lat':lat,'lon':lon,'timezone':zone,'hour':12,'minute':37}
                payload['selected_location']={key:payload[key] for key in ('city','region','country','lat','lon','timezone')}
                with patch('modules.ephemeris.Horizons',side_effect=RuntimeError('JPL disabled in tests')):
                    response=self.client.post('/natal-chart',json=payload,headers={**self.owner,'Origin':ORIGIN})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.headers.get('access-control-allow-origin'),ORIGIN)
                result=response.json()
                self.assertEqual(len(result['houses']),12)
                with self.sessions() as db:
                    chart=db.get(NatalChart,result['chart_id'])
                    stored=db.query(ChartData).filter_by(chart_id=chart.id).one()
                    self.assertEqual(chart.timezone,zone)
                    self.assertAlmostEqual(chart.hour,12+37/60)
                    self.assertEqual(stored.houses,result['houses'])
                    self.assertEqual(stored.house_system,'Placidus')
