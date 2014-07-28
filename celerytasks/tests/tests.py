from django.test import TestCase
from django.test.utils import override_settings
from django.core.urlresolvers import reverse
from django.conf import settings
import json
import random
from recharge.models import Recharge, RechargeError
from celerytasks.models import StoreToken
from celerytasks.tasks import (run_queries, hotsocket_login, get_recharge,
                               balance_query, balance_checker, send_kato_im_threshold_warning,
                               send_pushover_threshold_warning, resend_notification)
from gopherairtime.custom_exceptions import (TokenInvalidError, TokenExpireError,
                                             MSISDNNonNumericError, MSISDMalFormedError,
                                             BadProductCodeError, BadNetworkCodeError,
                                             BadCombinationError, DuplicateReferenceError,
                                             NonNumericReferenceError)
from users.models import GopherAirtimeAccount
from mock import patch

fixtures_global = ["test_auth_users.json", "test_projects.json", "test_recharge.json"]

class TestRecharge(TestCase):
    fixtures = fixtures_global

    @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS = True,
                       CELERY_ALWAYS_EAGER = True,
                       BROKER_BACKEND = 'memory',)

    def test_data_loaded(self):
        query = Recharge.objects.all()
        self.assertEqual(len(query), 5)

    # def test_query_function(self):
    #     run_queries.delay()
    #     query = Recharge.objects.all()
    #     [self.assertEqual(obj.status, settings.HS_RECHARGE_STATUS_CODES["PENDING"]["code"]) for obj in query]
    #     [self.assertIsNotNone(obj.reference) for obj in query]
    #     [self.assertIsNotNone(obj.recharge_system_ref) for obj in query]


    def test_recharge_success(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = random.randint(0, 999999999999999)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": query.msisdn,
                "product_code": query.product_code,
                "denomination": query.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query.id)
        query = Recharge.objects.get(msisdn=27821231232)
        self.assertIsNotNone(query.reference)
        self.assertIsNotNone(query.recharge_system_ref)
        self.assertEqual(settings.HS_RECHARGE_STATUS_CODES["PENDING"]["code"], query.status)

    def test_invalid_token(self):
        code = settings.HOTSOCKET_CODES
        reference = random.randint(0, 999999999999999)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": "x",
                "recipient_msisdn": query.msisdn,
                "product_code": query.product_code,
                "denomination": query.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query.id)
        query = Recharge.objects.get(msisdn=27821231232)
        self.assertIsNotNone(query.reference)
        self.assertIsNotNone(query.recharge_system_ref)
        self.assertEqual(settings.HS_RECHARGE_STATUS_CODES["PENDING"]["code"], query.status)

    def test_duplicate_reference(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = random.randint(0, 999999999999999)
        query_1 = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query_1.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                    "token": store_token.token,
                    "recipient_msisdn": query_1.msisdn,
                    "product_code": query_1.product_code,
                    "denomination": query_1.denomination,  # In cents
                    "network_code": "VOD",
                    "reference": reference,
                    "as_json": True}
        get_recharge.delay(data, query_1.id)
        query_1 = Recharge.objects.get(msisdn=27821231232)
        self.assertIsNotNone(query_1.reference)
        self.assertIsNotNone(query_1.recharge_system_ref)
        self.assertEqual(settings.HS_RECHARGE_STATUS_CODES["PENDING"]["code"], query_1.status)

        query_3 = Recharge.objects.get(msisdn=27821231231)

        self.assertIsNone(query_3.recharge_system_ref)

        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": query_3.msisdn,
                "product_code": query_3.product_code,
                "denomination": query_3.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query_3.id)
        query = Recharge.objects.get(msisdn=27821231231)
        self.assertIsNone(query.recharge_system_ref)

        error = RechargeError.objects.get(recharge_error=query.id)
        self.assertEqual(error.error_id, settings.HOTSOCKET_CODES["REF_DUPLICATE"]["status"])
        self.assertIsNotNone(error.last_attempt_at)

    def test_non_numeric_reference(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = "a"
        query_1 = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query_1.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": query_1.msisdn,
                "product_code": query_1.product_code,
                "denomination": query_1.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query_1.id)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        error = RechargeError.objects.get(recharge_error=query.id)
        self.assertEqual(error.error_id, settings.HOTSOCKET_CODES["REF_NON_NUM"]["status"])
        self.assertIsNotNone(error.last_attempt_at)

    def test_non_numeric_msisdn(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = random.randint(0, 999999999999999)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": "a",
                "product_code": query.product_code,
                "denomination": query.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query.id)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        error = RechargeError.objects.get(recharge_error=query.id)
        self.assertEqual(error.error_id, settings.HOTSOCKET_CODES["MSISDN_NON_NUM"]["status"])
        self.assertEqual(error.error_message, settings.HOTSOCKET_CODES["MSISDN_NON_NUM"]["message"])
        self.assertIsNotNone(error.last_attempt_at)

    def test_malformed_msisdn(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = random.randint(0, 999999999999999)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": 278,
                "product_code": query.product_code,
                "denomination": query.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query.id)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        error = RechargeError.objects.get(recharge_error=query.id)
        self.assertEqual(error.error_id, settings.HOTSOCKET_CODES["MSISDN_MALFORMED"]["status"])
        self.assertEqual(error.error_message, settings.HOTSOCKET_CODES["MSISDN_MALFORMED"]["message"])
        self.assertIsNotNone(error.last_attempt_at)

    def test_bad_product_code(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = random.randint(0, 999999999999999)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": query.msisdn,
                "product_code": "GOPHER",
                "denomination": query.denomination,  # In cents
                "network_code": "VOD",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query.id)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        error = RechargeError.objects.get(recharge_error=query.id)
        self.assertEqual(error.error_id, settings.HOTSOCKET_CODES["PRODUCT_CODE_BAD"]["status"])
        self.assertEqual(error.error_message, settings.HOTSOCKET_CODES["PRODUCT_CODE_BAD"]["message"])
        self.assertIsNotNone(error.last_attempt_at)


    def test_bad_network_code(self):
        code = settings.HOTSOCKET_CODES
        hotsocket_login()
        store_token = StoreToken.objects.get(id=1)
        reference = random.randint(0, 999999999999999)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        data = {"username": settings.HOTSOCKET_USERNAME,
                "token": store_token.token,
                "recipient_msisdn": query.msisdn,
                "product_code": query.product_code,
                "denomination": query.denomination,  # In cents
                "network_code": "GOPHER",
                "reference": reference,
                "as_json": True}
        get_recharge.delay(data, query.id)
        query = Recharge.objects.get(msisdn=27821231232)

        self.assertIsNone(query.recharge_system_ref)
        error = RechargeError.objects.get(recharge_error=query.id)
        self.assertEqual(error.error_id, settings.HOTSOCKET_CODES["NETWORK_CODE_BAD"]["status"])
        self.assertEqual(error.error_message, settings.HOTSOCKET_CODES["NETWORK_CODE_BAD"]["message"])
        self.assertIsNotNone(error.last_attempt_at)

    # @patch('celerytasks.sms_sender.VumiGoSender.send_sms')
    def test_resend_notification(self):
        with patch('celerytasks.sms_sender.VumiGoSender.send_sms') as mock_patch:
            msisdns = [27821231233, 27821231234]

            # Recharges for resend criteria
            recharges = Recharge.objects.filter(msisdn__in=msisdns).all()
            [self.assertFalse(obj.notification_sent) for obj in recharges]

            # Recharges NOT for resend criteria
            recharges_not = Recharge.objects.exclude(msisdn__in=msisdns).all()
            [self.assertFalse(obj.notification_sent) for obj in recharges_not]

            # Running the function
            resend_notification.delay()

            # Recharges for resend criteria
            recharges_not = Recharge.objects.exclude(msisdn__in=msisdns).all()
            [self.assertFalse(obj.notification_sent) for obj in recharges_not]

            # Recharges NOT for resend criteria
            recharges = Recharge.objects.filter(msisdn__in=msisdns).all()
            [self.assertTrue(obj.notification_sent) for obj in recharges]


class TestLogin(TestCase):
    @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS = True,
                       CELERY_ALWAYS_EAGER = True,
                       BROKER_BACKEND = 'memory',)


    def test_query_function(self):
        hotsocket_login()
        query = StoreToken.objects.all()
        [self.assertIsNotNone(obj.token) for obj in query]
        [self.assertIsNotNone(obj.updated_at) for obj in query]
        [self.assertIsNotNone(obj.expire_at) for obj in query]


# class TestBalanceQuery(TestCase):
#     @override_settings(CELERY_EAGER_PROPAGATES_EXCEPTIONS = True,
#                        CELERY_ALWAYS_EAGER = True,
#                        BROKER_BACKEND = 'memory',)

#     def test_balance_query(self):
#         balance_checker.delay()
#         account = GopherAirtimeAccount.objects.all()
#         self.assertEqual(type(account[0].running_balance), type(1))
#         self.assertIsNotNone(account[0].created_at)

    # def test_kato_im(self):
    #     send_kato_im_threshold_warning.delay(110)

    # def test_pushover(self):
    #     send_pushover_threshold_warning.delay(110)
