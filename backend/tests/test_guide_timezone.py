"""An account's guide can be shown in an IANA time zone, which follows daylight saving.

The fixed hour offset cannot: a guide set to +1 for Rome is an hour wrong all
summer. The zone is validated on the way in, so a typo is refused rather than
silently showing UTC.
"""
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pydantic import ValidationError

from api.accounts import AccountCreate, AccountUpdate


class GuideTimezoneValidationTests(unittest.TestCase):
    def _create(self, **kwargs):
        return AccountCreate(name="TV", server_url="https://p/pl.m3u8", provider_type="m3u", **kwargs)

    def test_known_zone_is_kept(self):
        self.assertEqual(self._create(guide_timezone=" Europe/Rome ").guide_timezone, "Europe/Rome")

    def test_unknown_zone_is_refused(self):
        with self.assertRaises(ValidationError):
            self._create(guide_timezone="Europe/Atlantis")

    def test_empty_means_none(self):
        self.assertIsNone(self._create(guide_timezone="").guide_timezone)
        self.assertIsNone(self._create().guide_timezone)

    def test_update_can_clear_it(self):
        update = AccountUpdate(guide_timezone="")
        self.assertIn("guide_timezone", update.model_dump(exclude_unset=True))
        self.assertIsNone(update.guide_timezone)


if __name__ == "__main__":
    unittest.main()
