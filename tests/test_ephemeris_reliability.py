"""Solar geometry, Placidus boundaries and one bounded JPL snapshot; no network."""
import io
import math
import unittest
from contextlib import redirect_stdout
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import patch
import ephem as independent_ephem
import swisseph as swe
import test_my_charts as f
from database.queries import NatalChart,ChartData
from modules.ephemeris import Ephemeris,UnsupportedHouseSystemError,JPL_ASTEROIDS,calculation_warnings
from modules.aspects import Aspects


class SolarGeometryTests(unittest.TestCase):
    def test_day_and_night_against_independent_ephem_in_both_hemispheres(self):
        fixtures=[(2000,6,21,h,37.62,55.75) for h in (0,6,12,18,22)]
        fixtures += [(2000,12,21,h,151.21,-33.87) for h in (2,8,14,20)]
        fixtures += [(2000,3,20,h,0,0) for h in (0,5,7,12,17,19)]
        for args in fixtures:
            with self.subTest(args=args):
                e=Ephemeris(*args);sun=swe.calc_ut(e.jd,swe.SUN,e.flags)[0]
                observer=independent_ephem.Observer();observer.lon=str(e.lon);observer.lat=str(e.lat)
                observer.pressure=0;observer.elevation=0
                observer.date=datetime(*args[:3])+timedelta(hours=args[3])
                altitude=math.degrees(float(independent_ephem.Sun(observer).alt))
                self.assertEqual(e.is_daytime(sun),altitude>=0)
                # Validate both representations use the same corrected sect/formula.
                asc=e._house_data()[1][0];moon=swe.calc_ut(e.jd,swe.MOON,e.flags)[0][0]
                expected=(asc+moon-sun[0] if altitude>=0 else asc+sun[0]-moon)%360
                self.assertAlmostEqual(e.get_key_points()['Часть Фортуны']['абс_долгота'],expected,places=9)
                self.assertAlmostEqual(e.get_key_point_longitudes()['Часть Фортуны'],expected,places=9)

    def test_original_moscow_noon_regression(self):
        e=Ephemeris(2000,6,21,12,37.62,55.75);sun=swe.calc_ut(e.jd,swe.SUN,e.flags)[0]
        self.assertTrue(e.is_daytime(sun));self.assertAlmostEqual(swe.azalt(e.jd,swe.ECL2HOR,(e.lon,e.lat,0),0,15,sun[:3])[1],47.7537274,places=5)

    def test_geometric_sun_centre_boundary_ignores_apparent_refraction_and_mc_wrap(self):
        e=Ephemeris(2000,1,1,12,0,0)
        for altitude,expected in [(-.01,False),(0.,True),(.01,True)]:
            with self.subTest(altitude=altitude),patch('modules.ephemeris.swe.azalt',return_value=(0,altitude,.5)):
                self.assertEqual(e.is_daytime((359.,0.,1.),(350.,355.)),expected)

    def test_real_sunrise_and_sunset_sides(self):
        # Equatorial equinox; find the geometric crossing, then check either side.
        for start,stop,rising in [(5.,7.,True),(17.,19.,False)]:
            for _ in range(35):
                hour=(start+stop)/2;e=Ephemeris(2000,3,20,hour,0,0)
                sun=swe.calc_ut(e.jd,swe.SUN,e.flags)[0]
                above=swe.azalt(e.jd,swe.ECL2HOR,(0,0,0),0,15,sun[:3])[1]>=0
                if above==rising:stop=hour
                else:start=hour
            for offset,expected in [(-1/60,not rising),(1/60,rising)]:
                e=Ephemeris(2000,3,20,(start+stop)/2+offset,0,0)
                self.assertEqual(e.is_daytime(swe.calc_ut(e.jd,swe.SUN,e.flags)[0]),expected)


class SnapshotTests(unittest.TestCase):
    def factory(self,fail_ids=(),clock=None):
        def create(id,**kwargs):
            def fetch():
                if clock is not None:clock[0]+=3
                if id in fail_ids:raise TimeoutError('https://provider/private-epoch-secret')
                return {'ObsEclLon':[int(id)%360+.25],'RA_rate':[-.5]}
            obj=SimpleNamespace(ephemerides=fetch,TIMEOUT=30)
            self.objects.append(obj);return obj
        self.objects=[];return create

    def test_six_queries_total_for_every_representation_and_aspects(self):
        e=Ephemeris(2000,6,21,12,37.62,55.75)
        with patch('modules.ephemeris.Horizons',side_effect=self.factory()) as query:
            bodies=e.get_all_bodies_with_degrees();e.get_all_bodies_with_symbols()
            aspects=Aspects(2000,6,21,12,37.62,55.75,ephemeris=e);e.get_all_bodies()
            self.assertEqual(query.call_count,6)
        self.assertEqual(calculation_warnings(bodies),[])
        for name,pos in e.get_asteroid_longitudes().items():
            self.assertEqual(aspects.objects[name],pos)
            self.assertEqual(e.get_asteroids()[name]['абс_долгота'],pos)
        self.assertTrue(all(0<obj.TIMEOUT<=3 for obj in self.objects))

    def test_failures_are_cached_and_do_not_log_epoch_or_provider_details(self):
        e=Ephemeris(2000,1,2,12,30,50)
        with patch('modules.ephemeris.Horizons',side_effect=self.factory(('2060',))) as query,self.assertLogs('modules.ephemeris',level='WARNING') as logs:
            bodies=e.get_all_bodies_with_degrees();e.get_asteroid_longitudes();e.get_all_bodies()
        self.assertEqual(query.call_count,6);self.assertNotIn('Хирон',e.get_asteroid_longitudes())
        self.assertIn('Хирон',calculation_warnings(bodies)[0]);self.assertIn('Церера',e.get_asteroid_longitudes())
        self.assertNotIn('private-epoch-secret',' '.join(logs.output))
        self.assertNotIn(str(e.jd),' '.join(logs.output))

    def test_budget_skips_later_queries_and_shrinks_timeouts(self):
        clock=[0.];e=Ephemeris(2000,1,2,12,30,50)
        with patch('modules.ephemeris.time.monotonic',side_effect=lambda:clock[0]),patch('modules.ephemeris.Horizons',side_effect=self.factory(clock=clock)) as query:
            with self.assertLogs('modules.ephemeris',level='WARNING'):
                result=e.get_asteroid_longitudes()
            self.assertEqual(query.call_count,4);self.assertEqual(len(result),4)
            self.assertEqual([obj.TIMEOUT for obj in self.objects],[3.,3.,3.,1.5])
            e.get_asteroid_longitudes();self.assertEqual(query.call_count,4)

    def test_invalid_provider_values_are_not_persistable(self):
        e=Ephemeris(2000,1,2,12,30,50)
        obj=SimpleNamespace(ephemerides=lambda:{'ObsEclLon':[float('nan')],'RA_rate':[0.]})
        with patch('modules.ephemeris.Horizons',return_value=obj),self.assertLogs('modules.ephemeris',level='WARNING'):
            self.assertEqual(e.get_asteroid_longitudes(),{})

    def test_two_calculations_do_not_share_coordinates_or_failed_snapshots(self):
        with patch('modules.ephemeris.Horizons',side_effect=self.factory()) as query:
            Ephemeris(2000,1,2,12,30,50).get_asteroid_longitudes()
            Ephemeris(2000,1,3,12,30,50).get_asteroid_longitudes()
        self.assertEqual(query.call_count,12)

    def test_house_result_is_reused_without_changing_native_cusps(self):
        e=Ephemeris(2000,1,2,12,30,50);original=swe.houses
        with patch('modules.ephemeris.swe.houses',wraps=original) as houses:
            self.assertEqual(list(e.get_houses().values()),list(original(e.jd,50,30,b'P')[0]))
            e.get_key_points();e.get_house_cusps();e.calculate_vertex()
        self.assertEqual(houses.call_count,1)

    def test_polar_error_does_not_disguise_an_unexpected_swiss_error(self):
        for lat in (68.97,-68.97,90,-90):
            with self.subTest(lat=lat),self.assertRaises(UnsupportedHouseSystemError):
                Ephemeris(2000,1,2,12,30,lat).get_houses()
        with patch('modules.ephemeris.swe.houses',side_effect=swe.Error('internal')):
            with self.assertRaises(swe.Error):Ephemeris(2000,1,2,12,30,50).get_houses()


class CalculationApiTests(unittest.TestCase):
    setUp=f.MyChartsTests.setUp
    tearDown=f.MyChartsTests.tearDown

    def test_polar_inputs_return_russian_422_with_cors_and_do_not_save(self):
        for lat in (68.97,-68.97):
            for headers in (self.owner,self.guest):
                with self.subTest(lat=lat,guest=headers==self.guest),patch('modules.ephemeris.Horizons') as query:
                    r=self.client.post('/natal-chart',json={**f.PAYLOAD,'lat':lat},headers={**headers,'Origin':'https://mylunariaai.com'})
                    self.assertEqual(r.status_code,422,r.text)
                    self.assertEqual(r.json()['detail']['code'],'UNSUPPORTED_HOUSE_SYSTEM')
                    self.assertIn('Плацидуса',r.json()['detail']['message'])
                    self.assertEqual(r.headers['access-control-allow-origin'],'https://mylunariaai.com');query.assert_not_called()
        with self.sessions() as db:
            self.assertEqual(db.query(NatalChart).count(),0);self.assertEqual(db.query(ChartData).count(),0)

    def test_partial_snapshot_is_consistent_and_warning_survives_reload(self):
        def factory(id,**kwargs):
            if id=='2060':raise TimeoutError('not logged')
            return SimpleNamespace(ephemerides=lambda:{'ObsEclLon':[int(id)%360+.25],'RA_rate':[.5]})
        with patch('modules.ephemeris.Horizons',side_effect=factory) as query:
            r=self.client.post('/natal-chart',json=f.PAYLOAD,headers=self.owner)
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(query.call_count,6)
        data=r.json();self.assertNotIn('⚷',data['bodies_for_circle']);self.assertIn('⚳',data['bodies_for_circle'])
        self.assertEqual(len(data['calculation_warnings']),1);self.assertIn('Хирон',data['calculation_warnings'][0])
        with patch('modules.ephemeris.Horizons') as query:
            saved=self.client.get('/natal-chart/'+str(data['chart_id']),headers=self.owner).json()
            query.assert_not_called()
        self.assertEqual(saved['calculation_warnings'],data['calculation_warnings'])
        self.assertEqual(saved['aspects_structured'],data['aspects_structured'])
        self.assertEqual(saved['bodies_for_circle'],data['bodies_for_circle'])

    def test_successful_full_chart_has_no_warning(self):
        obj=SimpleNamespace(ephemerides=lambda:{'ObsEclLon':[10.25],'RA_rate':[.5]})
        with patch('modules.ephemeris.Horizons',return_value=obj) as query:
            r=self.client.post('/natal-chart',json=f.PAYLOAD,headers=self.owner)
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(query.call_count,6)
        self.assertEqual(r.json()['calculation_warnings'],[])

if __name__=='__main__':unittest.main()
