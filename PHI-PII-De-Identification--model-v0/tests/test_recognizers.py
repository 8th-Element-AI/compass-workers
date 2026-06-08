import pytest

from deidentifier.recognizers.regex_recognizer import RegexRecognizer


@pytest.fixture(scope="module")
def recognizer():
    return RegexRecognizer()


class TestEmailPattern:
    def test_simple_email(self, recognizer):
        results = recognizer.analyze("Send to alice@example.com please.")
        matches = [r for r in results if r.entity_type == "EMAIL_ADDRESS"]
        assert len(matches) == 1
        assert matches[0].text == "alice@example.com"

    def test_email_with_subdomain(self, recognizer):
        results = recognizer.analyze("Contact hr@mail.company.org")
        matches = [r for r in results if r.entity_type == "EMAIL_ADDRESS"]
        assert any(r.text == "hr@mail.company.org" for r in matches)


class TestPhonePattern:
    def test_parenthetical_format(self, recognizer):
        results = recognizer.analyze("Call (800) 555-1234.")
        matches = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(matches) >= 1

    def test_dotted_format(self, recognizer):
        results = recognizer.analyze("Phone: 555.123.4567")
        matches = [r for r in results if r.entity_type == "PHONE_NUMBER"]
        assert len(matches) >= 1


class TestSSNPattern:
    def test_hyphen_format(self, recognizer):
        results = recognizer.analyze("SSN: 123-45-6789")
        matches = [r for r in results if r.entity_type == "US_SSN"]
        assert len(matches) == 1

    def test_space_format(self, recognizer):
        results = recognizer.analyze("Social: 987 65 4321")
        matches = [r for r in results if r.entity_type == "US_SSN"]
        assert len(matches) == 1


class TestCreditCardPattern:
    def test_visa(self, recognizer):
        results = recognizer.analyze("Card: 4532015112830366")
        matches = [r for r in results if r.entity_type == "CREDIT_CARD"]
        assert len(matches) == 1

    def test_amex(self, recognizer):
        results = recognizer.analyze("AMEX: 378282246310005")
        matches = [r for r in results if r.entity_type == "CREDIT_CARD"]
        assert len(matches) == 1


class TestIPAddress:
    def test_ipv4(self, recognizer):
        results = recognizer.analyze("IP: 192.168.1.100")
        matches = [r for r in results if r.entity_type == "IP_ADDRESS"]
        assert len(matches) == 1
        assert matches[0].text == "192.168.1.100"


class TestMRN:
    def test_mrn_prefix(self, recognizer):
        results = recognizer.analyze("MRN: 123456789")
        matches = [r for r in results if r.entity_type == "MEDICAL_RECORD_NUMBER"]
        assert len(matches) >= 1

    def test_mrn_hash_prefix(self, recognizer):
        results = recognizer.analyze("MRN-98765432")
        matches = [r for r in results if r.entity_type == "MEDICAL_RECORD_NUMBER"]
        assert len(matches) >= 1


class TestDateOfBirth:
    def test_dob_with_keyword(self, recognizer):
        results = recognizer.analyze("DOB: 03/15/1985")
        matches = [r for r in results if r.entity_type == "DATE_OF_BIRTH"]
        assert len(matches) >= 1

    def test_date_of_birth_iso(self, recognizer):
        results = recognizer.analyze("Date of Birth: 1990-07-22")
        matches = [r for r in results if r.entity_type == "DATE_OF_BIRTH"]
        assert len(matches) >= 1


class TestIBAN:
    def test_gb_iban(self, recognizer):
        results = recognizer.analyze("IBAN: GB29NWBK60161331926819")
        matches = [r for r in results if r.entity_type == "IBAN_CODE"]
        assert len(matches) >= 1


class TestZipCode:
    def test_zip_plus4_no_context_needed(self, recognizer):
        results = recognizer.analyze("ZIP: 90210-1234")
        matches = [r for r in results if r.entity_type == "ZIP_CODE"]
        assert len(matches) >= 1

    def test_zip_with_context_word(self, recognizer):
        results = recognizer.analyze("Zip code: 90210")
        matches = [r for r in results if r.entity_type == "ZIP_CODE"]
        assert len(matches) >= 1
