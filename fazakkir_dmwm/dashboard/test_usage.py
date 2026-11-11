from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from videos.models import Video

from .models import ActivityLog
from .worldmap import locate, pins_for


class UsageDashboardTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            "root", password="pw12345678", is_staff=True, is_superuser=True
        )
        self.video = Video.objects.create(
            user=self.admin,
            title="Kept",
            file_id="videos/kept.mp4",
            video_url="https://example.com/kept.mp4",
        )
        self.client.login(username="root", password="pw12345678")

    def log(self, kind, video=None, user=None, country="France", continent="Europe"):
        return ActivityLog.objects.create(
            kind=kind, video=video, user=user, country=country, continent=continent
        )

    def usage(self):
        return self.client.get(reverse("dashboard:usage"))

    def test_page_renders_when_a_watched_video_was_deleted(self):
        gone = Video.objects.create(
            user=self.admin,
            title="Gone",
            file_id="videos/gone.mp4",
            video_url="https://example.com/gone.mp4",
        )
        self.log(ActivityLog.KIND_VIEW, video=gone)
        self.log(ActivityLog.KIND_AUDIO, video=gone)
        gone.delete()

        response = self.usage()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deleted video")
        self.assertEqual(response.context["total_views"], 1)
        self.assertEqual(response.context["total_audio"], 1)

    def test_each_visitor_is_counted_once(self):
        visitor = User.objects.create_user("visitor", password="pw12345678")
        self.log(ActivityLog.KIND_VIEW, video=self.video, user=visitor)
        self.log(ActivityLog.KIND_AUDIO, video=self.video, user=visitor)

        response = self.usage()

        self.assertEqual(response.context["active_users"], 1)
        self.assertEqual(response.context["active_registered"], 1)

    def test_empty_day_renders(self):
        response = self.usage()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_users"], 0)
        self.assertContains(response, "No map locations for this day.")

    def test_visitors_are_placed_on_the_map_by_country(self):
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="France")
        self.log(ActivityLog.KIND_AUDIO, video=self.video, country="France")
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="Kenya", continent="Africa")

        response = self.usage()

        counts = {pin["name"]: pin["count"] for pin in response.context["map_pins"]}
        self.assertEqual(counts, {"France": 2, "Kenya": 1})
        self.assertEqual(response.context["map_unplaced"], 0)
        self.assertContains(response, 'class="usage-pin"')

    def test_lookup_spellings_are_matched_to_the_same_country(self):
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="Côte d'Ivoire", continent="Africa")
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="Ivory Coast", continent="Africa")
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="Vietnam", continent="Asia")

        response = self.usage()

        counts = {pin["name"]: pin["count"] for pin in response.context["map_pins"]}
        self.assertEqual(counts, {"Ivory Coast": 2, "Vietnam": 1})

    def test_requests_without_a_place_are_counted_but_not_pinned(self):
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="Unknown", continent="Unknown")
        self.log(ActivityLog.KIND_VIEW, video=self.video, country="Local", continent="Local")

        response = self.usage()

        self.assertEqual(response.context["map_pins"], [])
        self.assertEqual(response.context["map_unplaced"], 2)
        self.assertContains(response, "Some requests could not be placed on the map.")


class WorldMapTests(SimpleTestCase):
    def test_common_lookup_names_are_located(self):
        names = [
            "United States", "United Kingdom", "Russia", "South Korea", "Czechia",
            "Côte d'Ivoire", "Vietnam", "Taiwan", "Hong Kong", "Macau", "Kenya", "Réunion",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertIsNotNone(locate(name))

    def test_unknown_names_are_not_located(self):
        for name in ["", None, "Unknown", "Local", "Atlantis"]:
            with self.subTest(name=name):
                self.assertIsNone(locate(name))

    def test_position_is_a_fixed_projection_of_the_coordinates(self):
        pins, unplaced = pins_for([{"country": "France", "count": 1}])
        self.assertEqual(unplaced, 0)
        self.assertAlmostEqual(pins[0]["left"], (2 + 180) / 360 * 100, places=2)
        self.assertAlmostEqual(pins[0]["top"], (84 - 46) / 142 * 100, places=2)

    def test_the_largest_pin_has_the_largest_size(self):
        pins, _ = pins_for(
            [{"country": "France", "count": 4}, {"country": "Kenya", "count": 1}]
        )
        self.assertEqual(pins[0]["name"], "France")
        self.assertEqual(pins[0]["size"], 44.0)
        self.assertLess(pins[1]["size"], pins[0]["size"])
