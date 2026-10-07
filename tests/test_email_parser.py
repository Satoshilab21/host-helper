"""Regression tests for email_parser against Airbnb's text/plain format.

The fixture below mirrors the real structure of a "Reservation confirmed"
email as delivered by a direct Gmail forwarding rule -- UPPERCASE section
headers, 'Label   value' pairs on one line, two-column check-in/checkout, and
U+202F narrow no-break spaces in times -- with guest details scrubbed.

This is the armor against Airbnb template drift: if a future
template change breaks an extractor, the failing field names it exactly.
"""

import unittest

from host_helper.email_parser import parse_confirmation_email

SUBJECT = "Reservation confirmed - Jane Doe arrives Aug 10"

#   is the narrow no-break space Airbnb puts before AM/PM.
BODY = """Airbnb

New booking confirmed! Jane arrives Aug 10.

Send a message to confirm check-in details or welcome Jane.

Jane Doe

Identity verified

Springfield, IL

Hi Host,

Looking forward to the stay!

EXAMPLE VACATION HOME

Entire home/apt

[https://www.airbnb.com/rooms/12345678?c=example&euid=example]

Check-in      Checkout
              \n\
Mon, Aug 10   Sun, Aug 16
              \n\
4:00 PM       10:00 AM

GUESTS

3 adults, 1 child

MORE DETAILS ABOUT WHO'S COMING

CONFIRMATION CODE
HMEXAMPLE1

GUEST PAID

$200.00 x 6 nights   $1,200.00

Cleaning fee   $180.00

Guest service fee   $0.00

Occupancy taxes   $120.00

TOTAL (USD)   $1,500.00

HOST PAYOUT

6 nights room fee   $1,500.00

Cleaning fee   $200.00

Nightly rate adjustment   -$300.00

Host service fee (15.5%)   -$217.00

YOU EARN   $1,183.00

Your guest paid $120.00 in Occupancy Taxes. Airbnb remits these taxes.

Your cancellation policy for guests is Firm
"""

EXPECTED = {
    "confirmation_code": "HMEXAMPLE1",
    "guest_name": "Jane Doe",
    "adults": 3,
    "children": 1,
    "infants": 0,
    "checkin_time": "4:00 PM",
    "checkout_time": "10:00 AM",
    "payout_amount": "$1,183.00",
    "listing_name": "EXAMPLE VACATION HOME",
    "listing_id": "12345678",
    "guest_location": "Springfield, IL",
    "identity_verified": True,
    "cancellation_policy": "Firm",
    "nightly_rate": "$200.00",
    "nights": 6,
    # Deliberately differs from the guest-side $180.00 above: this must come
    # from the HOST PAYOUT section, not the first "Cleaning fee" match.
    "cleaning_fee": "$200.00",
    "guest_service_fee": "$0.00",
    "occupancy_taxes": "$120.00",
    "total_guest_paid": "$1,500.00",
    "room_fee": "$1,500.00",
    "host_service_fee": "-$217.00",
}


class EmailParserTests(unittest.TestCase):
    def test_empty_body_degrades_to_missing_values(self):
        blank = parse_confirmation_email("", "")
        self.assertTrue(all(value is None for key, value in blank.items() if key != "identity_verified"))


def _field_test(field, expected):
    def test(self):
        self.assertEqual(parse_confirmation_email(SUBJECT, BODY).get(field), expected)

    return test


for _field, _expected in EXPECTED.items():
    setattr(EmailParserTests, f"test_{_field}", _field_test(_field, _expected))
