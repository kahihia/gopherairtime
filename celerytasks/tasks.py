# Python
import datetime
import json

# Django
from django.template.loader import get_template
from django.template import Context
from django.utils import timezone
from django.conf import settings
from django.db.models import Q

# Project
from gopherairtime.custom_exceptions import (TokenInvalidError, TokenExpireError,
                                             MSISDNNonNumericError, MSISDMalFormedError,
                                             BadProductCodeError, BadNetworkCodeError,
                                             BadCombinationError, DuplicateReferenceError,
                                             NonNumericReferenceError, SentryException)
from recharge.models import Recharge, RechargeError, RechargeFailed
from libs.shareddefs import send_mandrill_email
from users.models import GopherAirtimeAccount
from celerytasks.models import StoreToken
from celerytasks.sms_sender import VumiGoSender

# Celery
from celery.utils.log import get_task_logger
from celery.decorators import task

# Third Party
import requests


logger = get_task_logger(__name__)
CHECK_STATUS = settings.HS_RECHARGE_STATUS_CODES

@task
def hotsocket_login():
	data = {
			    "username": settings.HOTSOCKET_USERNAME,
			    "password": settings.HOTSOCKET_PASSWORD,
			    "as_json": True
			}

	url = "%s%s" % (settings.HOTSOCKET_BASE, settings.HOTSOCKET_RESOURCES["login"])
	response = requests.post(url, data=data)
	json_response = response.json()

	if str(json_response["response"]["status"]) == "0000":
		# Assuming the token will always be at primary key one
		updated_at = timezone.now()
		expire_at = updated_at + datetime.timedelta(minutes=settings.TOKEN_DURATION)
		if not StoreToken.objects.filter(id=1).exists():
			store = StoreToken(token=json_response["response"]["token"],
			                   updated_at=updated_at,
			                   expire_at=expire_at,
			                   pk=1)
			store.save()
		else:
			query = StoreToken.objects.get(id=1)
			query.token = json_response["response"]["token"]
			query.updated_at = updated_at
			query.expire_at = expire_at
			query.save()


# =============================================================================
#	Balance Checker Code
# =============================================================================
@task
def balance_checker():
	# Try get the stored token if not there
	logger.info("Performing balance query")
	try:
		store_token = StoreToken.objects.get(id=1)
		data = {"username": settings.HOTSOCKET_USERNAME,
				"token": store_token.token,
				"as_json": True}
		balance_query.delay(data)

	# If it is the first time its running do the hotsocket login again
	except StoreToken.DoesNotExist, exc:
		logger.warning("Store token not valid, trying hotsocket login again")
		# If the hotsocket_login is ready run balance checker again.
		if hotsocket_login.delay().ready():
			balance_checker.retry(exc=exc)

@task
def balance_query(data):
	code = settings.HOTSOCKET_CODES
	url = "%s%s" % (settings.HOTSOCKET_BASE, settings.HOTSOCKET_RESOURCES["balance"])
	try:
		response = requests.post(url, data=data)
		json_response = response.json()
		status = json_response["response"]["status"]
		message = json_response["response"]["message"]

		if str(status) == code["SUCCESS"]["status"]:
			# if status =="0000" storing updating the balance
			balance = json_response["response"]["running_balance"]
			account = GopherAirtimeAccount(running_balance=balance)
			account.save()

			# Checking if balance is below threshold
			if balance < settings.THRESHOLD_WARNING_LEVEL:
				low_balance_warning.delay(balance)
		else:
			# If status != "0000": raise sentry error
			raise SentryException("Checking balance failed with"
			                	  "flickswitch status code %s and message %s "
			                	  % (status, message))


	except (TokenInvalidError, TokenExpireError), exc:
		if hotsocket_login.delay().ready():
			balance_query.retry(exc=exc)


@task
def low_balance_warning(balance):
	logger.info("Running the low balance warning notifier")
	send_email_threshold_warning.delay(balance)
	send_kato_im_threshold_warning.delay(balance)
	send_pushover_threshold_warning.delay(balance)


# Mandrill email
@task
def send_email_threshold_warning(balance):
	logger.info("Notifying low balance by e-mail")
	context_email = {"balance": balance}
	subject = "Balance Running Low"
	email = settings.ADMIN_EMAIL["threshold_limit"]
	html = get_template("email/email_threshold_notify.html").render(Context(context_email))
	text = get_template("email/email_threshold_notify.txt").render(Context(context_email))
	send_mandrill_email(html, text, subject, [{"email": email}])


# Kato IM
@task
def send_kato_im_threshold_warning(balance):
	logger.info("Notifying low balance by kato")
	headers = {'content-type': 'application/json'}
	data = {"from": "GopherAirtime",
			 "color": "red",
			 "renderer": "markdown",
			 "text": "Balance is currently: %s" % balance}
	response = requests.post("https://api.kato.im/rooms/%s/simple" % settings.KATO_KEY,
	                         data=json.dumps(data),
	                         headers=headers)

# PUSHOVER
@task
def send_pushover_threshold_warning(balance):
	logger.info("Notifying low balance by pushover")
	data = {"token": settings.PUSHOVER_APP,
			"user": settings.PUSHOVER_USERS["mike"],
			"message": "Balance is currently: %s" % balance}
	response = requests.post(settings.PUSHOVER_MESSAGE_URL, data=data)

# End Balance Checker Code
# =============================================================================


# =============================================================================
#	Running Recharge and Status Query
# =============================================================================

@task
def run_queries():
	"""
	Main purpose of this is to call functions that query database and to chain them
	"""
	logger.info("Running database query")
	recharge_query.delay()
	status_query.delay()
	errors_query.delay()
	resend_notification.delay()


@task
def recharge_query():
	"""
	Queries database and passes it to the get_recharge() task asynchronously
	"""
	error_list = []
	try:
		# Storing the recharge token in the database
		store_token = StoreToken.objects.get(id=1)
		# Getting recharges where the status is none
		queryset = Recharge.objects.filter(status=None).all()
		for query in queryset:
			# Checking if the limit has been exceeded
			limit = query.recharge_project.recharge_limit
			if query.denomination > limit:
				# if limit exceeded add to logger and error list, add to error_list and skip
				# entry
				logger.error("Recharge limit exceeded for %s", query.msisdn)
				error_list.append(query.id)
				continue

			data = {"username": settings.HOTSOCKET_USERNAME,
					"token": store_token.token,
					"recipient_msisdn": query.msisdn,
					"product_code": query.product_code,
					"denomination": query.denomination,  # In cents
					"network_code": query_network(query.msisdn),
					"reference": query.reference,
					"as_json": True}
			# seting the status to -1 to indicate that the task is already running,
			# used to prevent task from re-running
			query.status = -1
			query.save()
			get_recharge.delay(data, query.id)

	# If it is the first time its running do the hotsocket login again
	except StoreToken.DoesNotExist, exc:
		hotsocket_login.delay()
		recharge_query.retry(countdown=20, exc=exc)

	finally:
		# The threshhold exceeded error is 404
		# Getting all the error_ids and storing it in the database.
		if error_list:
			for _id in error_list:
				error = RechargeError(error_id=settings.INTERNAL_ERROR["LIMIT_REACHED"]["status"],
				                      error_message=settings.INTERNAL_ERROR["LIMIT_REACHED"]["message"],
				                      last_attempt_at=timezone.now(),
				                      recharge_error_id=_id,
				                      tries=1)
			error.save()

			update_recharge = Recharge.objects.get(id=_id)
			update_recharge.status = settings.INTERNAL_ERROR["LIMIT_REACHED"]["status"]
			update_recharge.save()


@task
def status_query():
	"""
	Queries database to check if status is null and recharge error and reference is not null
	"""
	logger.info("Running status query")
	try:
		store_token = StoreToken.objects.get(id=1)
		queryset = Recharge.objects.filter(status=0).all()
		for query in queryset:
			data = {"username": settings.HOTSOCKET_USERNAME,
					"token": store_token.token,
					"reference": query.reference,
					"as_json": True}
			check_recharge_status.delay(data, query.id)
	except StoreToken.DoesNotExist, exc:
		hotsocket_login.delay()
		recharge_query.retry(countdown=20, exc=exc)


@task
def errors_query():
	pass


@task()
def get_recharge(data, query_id):
		logger.info("Running get recharge for %s" % query_id)
		url = "%s%s" % (settings.HOTSOCKET_BASE, settings.HOTSOCKET_RESOURCES["recharge"])
		code = settings.HOTSOCKET_CODES
		query = Recharge.objects.get(id=query_id)

		try:
			response = requests.post(url, data=data)
			json_response = response.json()
			status = json_response["response"]["status"]
			message = json_response["response"]["message"]
			if str(status) == code["SUCCESS"]["status"]:
				query.reference = data["reference"]
				query.recharge_system_ref = json_response["response"]["hotsocket_ref"]
				query.status = CHECK_STATUS["PENDING"]["code"]
				query.status_confirmed_at = timezone.now()
				query.save()

			elif status == code["REF_DUPLICATE"]["status"]:
				raise DuplicateReferenceError(message)

			elif status == code["REF_NON_NUM"]["status"]:
				raise NonNumericReferenceError(message)

			elif status == code["TOKEN_EXPIRE"]["status"]:
				raise TokenExpireError(message)

			elif status == code["TOKEN_INVALID"]["status"]:
				raise TokenInvalidError(message)

			elif status == code["MSISDN_NON_NUM"]["status"]:
				raise MSISDNNonNumericError(message)

			elif status == code["MSISDN_MALFORMED"]["status"]:
				raise MSISDMalFormedError(message)

			elif status == code["PRODUCT_CODE_BAD"]["status"]:
				raise BadProductCodeError(message)

			elif status == code["NETWORK_CODE_BAD"]["status"]:
				raise BadNetworkCodeError(message)

			elif status == code["COMBO_BAD"]["status"]:
				raise BadCombinationError(message)

		except (TokenInvalidError, TokenExpireError), exc:
			if hotsocket_login.delay().ready():
				store_token = StoreToken.objects.get(id=1)
				data["token"] = store_token.token
				get_recharge.retry(args=[data, query_id], exc=exc)

		except (MSISDNNonNumericError, MSISDMalFormedError, BadProductCodeError,
		        BadNetworkCodeError, BadCombinationError, DuplicateReferenceError, NonNumericReferenceError), exc:
			error = RechargeError(error_id=status,
			                      error_message=message,
			                      last_attempt_at=timezone.now(),
			                      recharge_error=query,
			                      tries=1)
			error.save()

			update_recharge = Recharge.objects.get(id=query_id)
			update_recharge.status = CHECK_STATUS["PRE_SUB_ERROR"]["code"]
			update_recharge.status_confirmed_at = timezone.now()
			update_recharge.save()


@task
def check_recharge_status(data, query_id):
		url = "%s%s" % (settings.HOTSOCKET_BASE, settings.HOTSOCKET_RESOURCES["status"])
		code = settings.HOTSOCKET_CODES
		query = Recharge.objects.get(id=query_id)
		logger.info("Checking the status for %s" % query_id)

		try:
			response = requests.post(url, data=data)
			json_response = response.json()
			status = json_response["response"]["status"]
			message = json_response["response"]["message"]
			recharge_status_code = json_response["response"]["recharge_status_cd"]

			if str(status) == str(code["SUCCESS"]["status"]):
				query.status = int(recharge_status_code)
				query.status_confirmed_at = timezone.now()
				query.save()

				if int(recharge_status_code) == 3:

					# Updating the account balance after each query
					balance = json_response["response"]["running_balance"]
					account = GopherAirtimeAccount(running_balance=balance)
					account.save()
					# Notify the recipient via SMS
					if query.notification:
						send_sms.delay(query.msisdn,
						               query.notification,
						               query.recharge_project.account_id,
						               query.recharge_project.conversation_id,
						               query.recharge_project.conversation_token)
						query.notification_sent = True
						query.save()


			elif status == code["TOKEN_EXPIRE"]["status"]:
				raise TokenExpireError(message)

			elif status == code["TOKEN_INVALID"]["status"]:
				raise TokenInvalidError(message)

			if int(recharge_status_code) == CHECK_STATUS["FAILED"]["code"]:
				failure = RechargeFailed(recharge_failed=query,
				                         recharge_status=json_response["response"]["recharge status"],
				                         failure_message=message
				                         )
				failure.save()

		except (TokenInvalidError, TokenExpireError), exc:
			if hotsocket_login.delay().ready():
				store_token = StoreToken.objects.get(id=1)
				data["token"] = store_token.token
				check_recharge_status.retry(args=[data, query_id], exc=exc)

		except Exception as e:
			error = RechargeError(error_id=status,
			                      error_message=message,
			                      last_attempt_at=timezone.now(),
			                      recharge_error=query,
			                      tries=1)
			error.save()

			update_recharge = Recharge.objects.get(id=query_id)
			update_recharge.status = CHECK_STATUS["PRE_SUB_ERROR"]["code"]
			update_recharge.status_confirmed_at = timezone.now()
			update_recharge.save()


def query_network(msisdn):
    mapping = (
        ('2783', 'MTN'),
        ('2773', 'MTN'),
        ('2778', 'MTN'),
        ('27710', 'MTN'),
        ('27717', 'MTN'),
        ('27718', 'MTN'),
        ('27719', 'MTN'),
        ('2782', 'VOD'),
        ('2772', 'VOD'),
        ('2776', 'VOD'),
        ('2779', 'VOD'),
        ('27711', 'VOD'),
        ('27712', 'VOD'),
        ('27713', 'VOD'),
        ('27714', 'VOD'),
        ('27715', 'VOD'),
        ('27716', 'VOD'),
        ('2784', 'CELLC'),
        ('2774', 'CELLC'),
        ('27811', '8TA'),
        ('27812', '8TA'),
        ('27813', '8TA'),
        ('27814', '8TA'),
        )

    for prefix, op in mapping:
        if str(msisdn).startswith(prefix):
            return op
    return None


@task
def resend_notification():
	queryset = (Recharge.objects.
	            exclude(notification__isnull=True).
	            exclude(notification__exact='').
	            filter(status=3).
	            filter(Q(notification_sent=None) | Q(notification_sent=False)).all())

	for query in queryset:
		send_sms.delay(query.msisdn,
		               query.notification,
		               query.recharge_project.account_id,
		               query.recharge_project.conversation_id,
		               query.recharge_project.conversation_token)
		query.notification_sent = True
		query.save()

#	Recharge and status query end
# =============================================================================


# ==========================================================
	#  VumiGoSender
# ==========================================================
@task
def send_sms(msisdn, sms, account_id, conversation_id, conversation_token):
	sender = VumiGoSender(api_url=settings.VUMIGO_API_URL,
	                   account_id=account_id,
	                   conversation_id=conversation_id,
	                   conversation_token=conversation_token)
	sender.send_sms(msisdn, sms)
