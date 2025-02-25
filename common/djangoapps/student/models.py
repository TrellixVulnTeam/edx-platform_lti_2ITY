"""
Models for User Information (students, staff, etc)

Migration Notes

If you make changes to this model, be sure to create an appropriate migration
file and check it in at the same time as your model changes. To do that,

1. Go to the edx-platform dir
2. ./manage.py lms schemamigration student --auto description_of_your_change
3. Add the migration file created in edx-platform/common/djangoapps/student/migrations/
"""
from datetime import datetime, timedelta
import hashlib
import json
import logging
from pytz import UTC
import uuid
from collections import defaultdict
import dogstats_wrapper as dog_stats_api
from django.db.models import Q
import pytz

from django.conf import settings
from django.utils import timezone
from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db import models, IntegrityError, transaction
from django.db.models import Count
from django.dispatch import receiver, Signal
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import ugettext_noop
from django_countries.fields import CountryField
from config_models.models import ConfigurationModel
from track import contexts
from eventtracking import tracker
from importlib import import_module

from opaque_keys.edx.locations import SlashSeparatedCourseKey
from xmodule.modulestore import Location
from opaque_keys import InvalidKeyError

import lms.lib.comment_client as cc
from util.query import use_read_replica_if_available
from xmodule_django.models import CourseKeyField, NoneToEmptyManager
from xmodule.modulestore.exceptions import ItemNotFoundError
from xmodule.modulestore.django import modulestore
from opaque_keys.edx.keys import CourseKey
from functools import total_ordering

from certificates.models import GeneratedCertificate
from course_modes.models import CourseMode

from ratelimitbackend import admin

import analytics

UNENROLL_DONE = Signal(providing_args=["course_enrollment", "skip_refund"])
log = logging.getLogger(__name__)
AUDIT_LOG = logging.getLogger("audit")
SessionStore = import_module(settings.SESSION_ENGINE).SessionStore  # pylint: disable=invalid-name


class AnonymousUserId(models.Model):
    """
    This table contains user, course_Id and anonymous_user_id

    Purpose of this table is to provide user by anonymous_user_id.

    We generate anonymous_user_id using md5 algorithm,
    and use result in hex form, so its length is equal to 32 bytes.
    """

    objects = NoneToEmptyManager()

    user = models.ForeignKey(User, db_index=True)
    anonymous_user_id = models.CharField(unique=True, max_length=32)
    course_id = CourseKeyField(db_index=True, max_length=255, blank=True)
    unique_together = (user, course_id)


def anonymous_id_for_user(user, course_id, save=True):
    """
    Return a unique id for a (user, course) pair, suitable for inserting
    into e.g. personalized survey links.

    If user is an `AnonymousUser`, returns `None`

    Keyword arguments:
    save -- Whether the id should be saved in an AnonymousUserId object.
    """
    # This part is for ability to get xblock instance in xblock_noauth handlers, where user is unauthenticated.
    if user.is_anonymous():
        return None

    cached_id = getattr(user, '_anonymous_id', {}).get(course_id)
    if cached_id is not None:
        return cached_id

    # include the secret key as a salt, and to make the ids unique across different LMS installs.
    hasher = hashlib.md5()
    hasher.update(settings.SECRET_KEY)
    hasher.update(unicode(user.id))
    if course_id:
        hasher.update(course_id.to_deprecated_string().encode('utf-8'))
    digest = hasher.hexdigest()

    if not hasattr(user, '_anonymous_id'):
        user._anonymous_id = {}  # pylint: disable=protected-access

    user._anonymous_id[course_id] = digest  # pylint: disable=protected-access

    if save is False:
        return digest

    try:
        anonymous_user_id, __ = AnonymousUserId.objects.get_or_create(
            defaults={'anonymous_user_id': digest},
            user=user,
            course_id=course_id
        )
        if anonymous_user_id.anonymous_user_id != digest:
            log.error(
                "Stored anonymous user id {stored!r} for user {user!r} "
                "in course {course!r} doesn't match computed id {digest!r}".format(
                    user=user,
                    course=course_id,
                    stored=anonymous_user_id.anonymous_user_id,
                    digest=digest
                )
            )
    except IntegrityError:
        # Another thread has already created this entry, so
        # continue
        pass

    return digest


def user_by_anonymous_id(uid):
    """
    Return user by anonymous_user_id using AnonymousUserId lookup table.

    Do not raise `django.ObjectDoesNotExist` exception,
    if there is no user for anonymous_student_id,
    because this function will be used inside xmodule w/o django access.
    """

    if uid is None:
        return None

    try:
        return User.objects.get(anonymoususerid__anonymous_user_id=uid)
    except ObjectDoesNotExist:
        return None


class UserStanding(models.Model):
    """
    This table contains a student's account's status.
    Currently, we're only disabling accounts; in the future we can imagine
    taking away more specific privileges, like forums access, or adding
    more specific karma levels or probationary stages.
    """
    ACCOUNT_DISABLED = "disabled"
    ACCOUNT_ENABLED = "enabled"
    USER_STANDING_CHOICES = (
        (ACCOUNT_DISABLED, u"Account Disabled"),
        (ACCOUNT_ENABLED, u"Account Enabled"),
    )

    user = models.ForeignKey(User, db_index=True, related_name='standing', unique=True)
    account_status = models.CharField(
        blank=True, max_length=31, choices=USER_STANDING_CHOICES
    )
    changed_by = models.ForeignKey(User, blank=True)
    standing_last_changed_at = models.DateTimeField(auto_now=True)


class UserProfile(models.Model):
    """This is where we store all the user demographic fields. We have a
    separate table for this rather than extending the built-in Django auth_user.

    Notes:
        * Some fields are legacy ones from the first run of 6.002, from which
          we imported many users.
        * Fields like name and address are intentionally open ended, to account
          for international variations. An unfortunate side-effect is that we
          cannot efficiently sort on last names for instance.

    Replication:
        * Only the Portal servers should ever modify this information.
        * All fields are replicated into relevant Course databases

    Some of the fields are legacy ones that were captured during the initial
    MITx fall prototype.
    """

    class Meta:  # pylint: disable=missing-docstring
        db_table = "auth_userprofile"

    # CRITICAL TODO/SECURITY
    # Sanitize all fields.
    # This is not visible to other users, but could introduce holes later
    user = models.OneToOneField(User, unique=True, db_index=True, related_name='profile')
    name = models.CharField(blank=True, max_length=255, db_index=True)

    meta = models.TextField(blank=True)  # JSON dictionary for future expansion
    courseware = models.CharField(blank=True, max_length=255, default='course.xml')

    # Location is no longer used, but is held here for backwards compatibility
    # for users imported from our first class.
    language = models.CharField(blank=True, max_length=255, db_index=True)
    location = models.CharField(blank=True, max_length=255, db_index=True)

    # Optional demographic data we started capturing from Fall 2012
    this_year = datetime.now(UTC).year
    VALID_YEARS = range(this_year, this_year - 120, -1)
    year_of_birth = models.IntegerField(blank=True, null=True, db_index=True)
    GENDER_CHOICES = (
        ('m', ugettext_noop('Male')),
        ('f', ugettext_noop('Female')),
        # Translators: 'Other' refers to the student's gender
        ('o', ugettext_noop('Other'))
    )
    gender = models.CharField(
        blank=True, null=True, max_length=6, db_index=True, choices=GENDER_CHOICES
    )

    # [03/21/2013] removed these, but leaving comment since there'll still be
    # p_se and p_oth in the existing data in db.
    # ('p_se', 'Doctorate in science or engineering'),
    # ('p_oth', 'Doctorate in another field'),
    LEVEL_OF_EDUCATION_CHOICES = (
        ('p', ugettext_noop('Doctorate')),
        ('m', ugettext_noop("Master's or professional degree")),
        ('b', ugettext_noop("Bachelor's degree")),
        ('a', ugettext_noop("Associate's degree")),
        ('hs', ugettext_noop("Secondary/high school")),
        ('jhs', ugettext_noop("Junior secondary/junior high/middle school")),
        ('el', ugettext_noop("Elementary/primary school")),
        # Translators: 'None' refers to the student's level of education
        ('none', ugettext_noop("None")),
        # Translators: 'Other' refers to the student's level of education
        ('other', ugettext_noop("Other"))
    )
    level_of_education = models.CharField(
        blank=True, null=True, max_length=6, db_index=True,
        choices=LEVEL_OF_EDUCATION_CHOICES
    )
    mailing_address = models.TextField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    country = CountryField(blank=True, null=True)
    goals = models.TextField(blank=True, null=True)
    allow_certificate = models.BooleanField(default=1)

    def get_meta(self):  # pylint: disable=missing-docstring
        js_str = self.meta
        if not js_str:
            js_str = dict()
        else:
            js_str = json.loads(self.meta)

        return js_str

    def set_meta(self, meta_json):  # pylint: disable=missing-docstring
        self.meta = json.dumps(meta_json)

    def set_login_session(self, session_id=None):
        """
        Sets the current session id for the logged-in user.
        If session_id doesn't match the existing session,
        deletes the old session object.
        """
        meta = self.get_meta()
        old_login = meta.get('session_id', None)
        if old_login:
            SessionStore(session_key=old_login).delete()
        meta['session_id'] = session_id
        self.set_meta(meta)
        self.save()

    @transaction.commit_on_success
    def update_name(self, new_name):
        """Update the user's name, storing the old name in the history.

        Implicitly saves the model.
        If the new name is not the same as the old name, do nothing.

        Arguments:
            new_name (unicode): The new full name for the user.

        Returns:
            None

        """
        if self.name == new_name:
            return

        if self.name:
            meta = self.get_meta()
            if 'old_names' not in meta:
                meta['old_names'] = []
            meta['old_names'].append([self.name, u"", datetime.now(UTC).isoformat()])
            self.set_meta(meta)

        self.name = new_name
        self.save()

    @transaction.commit_on_success
    def update_email(self, new_email):
        """Update the user's email and save the change in the history.

        Implicitly saves the model.
        If the new email is the same as the old email, do not update the history.

        Arguments:
            new_email (unicode): The new email for the user.

        Returns:
            None
        """
        if self.user.email == new_email:
            return

        meta = self.get_meta()
        if 'old_emails' not in meta:
            meta['old_emails'] = []
        meta['old_emails'].append([self.user.email, datetime.now(UTC).isoformat()])
        self.set_meta(meta)
        self.save()

        self.user.email = new_email
        self.user.save()


class UserSignupSource(models.Model):
    """
    This table contains information about users registering
    via Micro-Sites
    """
    user = models.ForeignKey(User, db_index=True)
    site = models.CharField(max_length=255, db_index=True)


def unique_id_for_user(user, save=True):
    """
    Return a unique id for a user, suitable for inserting into
    e.g. personalized survey links.

    Keyword arguments:
    save -- Whether the id should be saved in an AnonymousUserId object.
    """
    # Setting course_id to '' makes it not affect the generated hash,
    # and thus produce the old per-student anonymous id
    return anonymous_id_for_user(user, None, save=save)


# TODO: Should be renamed to generic UserGroup, and possibly
# Given an optional field for type of group
class UserTestGroup(models.Model):
    users = models.ManyToManyField(User, db_index=True)
    name = models.CharField(blank=False, max_length=32, db_index=True)
    description = models.TextField(blank=True)


class Registration(models.Model):
    ''' Allows us to wait for e-mail before user is registered. A
        registration profile is created when the user creates an
        account, but that account is inactive. Once the user clicks
        on the activation key, it becomes active. '''
    class Meta:
        db_table = "auth_registration"

    user = models.ForeignKey(User, unique=True)
    activation_key = models.CharField(('activation key'), max_length=32, unique=True, db_index=True)

    def register(self, user):
        # MINOR TODO: Switch to crypto-secure key
        self.activation_key = uuid.uuid4().hex
        self.user = user
        self.save()

    def activate(self):
        self.user.is_active = True
        self.user.save()


class PendingNameChange(models.Model):
    user = models.OneToOneField(User, unique=True, db_index=True)
    new_name = models.CharField(blank=True, max_length=255)
    rationale = models.CharField(blank=True, max_length=1024)


class PendingEmailChange(models.Model):
    user = models.OneToOneField(User, unique=True, db_index=True)
    new_email = models.CharField(blank=True, max_length=255, db_index=True)
    activation_key = models.CharField(('activation key'), max_length=32, unique=True, db_index=True)

    def request_change(self, email):
        """Request a change to a user's email.

        Implicitly saves the pending email change record.

        Arguments:
            email (unicode): The proposed new email for the user.

        Returns:
            unicode: The activation code to confirm the change.

        """
        self.new_email = email
        self.activation_key = uuid.uuid4().hex
        self.save()
        return self.activation_key


EVENT_NAME_ENROLLMENT_ACTIVATED = 'edx.course.enrollment.activated'
EVENT_NAME_ENROLLMENT_DEACTIVATED = 'edx.course.enrollment.deactivated'
EVENT_NAME_ENROLLMENT_MODE_CHANGED = 'edx.course.enrollment.mode_changed'


class PasswordHistory(models.Model):
    """
    This model will keep track of past passwords that a user has used
    as well as providing contraints (e.g. can't reuse passwords)
    """
    user = models.ForeignKey(User)
    password = models.CharField(max_length=128)
    time_set = models.DateTimeField(default=timezone.now)

    def create(self, user):
        """
        This will copy over the current password, if any of the configuration has been turned on
        """

        if not (PasswordHistory.is_student_password_reuse_restricted() or
                PasswordHistory.is_staff_password_reuse_restricted() or
                PasswordHistory.is_password_reset_frequency_restricted() or
                PasswordHistory.is_staff_forced_password_reset_enabled() or
                PasswordHistory.is_student_forced_password_reset_enabled()):

            return

        self.user = user
        self.password = user.password
        self.save()

    @classmethod
    def is_student_password_reuse_restricted(cls):
        """
        Returns whether the configuration which limits password reuse has been turned on
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return False
        min_diff_pw = settings.ADVANCED_SECURITY_CONFIG.get(
            'MIN_DIFFERENT_STUDENT_PASSWORDS_BEFORE_REUSE', 0
        )
        return min_diff_pw > 0

    @classmethod
    def is_staff_password_reuse_restricted(cls):
        """
        Returns whether the configuration which limits password reuse has been turned on
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return False
        min_diff_pw = settings.ADVANCED_SECURITY_CONFIG.get(
            'MIN_DIFFERENT_STAFF_PASSWORDS_BEFORE_REUSE', 0
        )
        return min_diff_pw > 0

    @classmethod
    def is_password_reset_frequency_restricted(cls):
        """
        Returns whether the configuration which limits the password reset frequency has been turned on
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return False
        min_days_between_reset = settings.ADVANCED_SECURITY_CONFIG.get(
            'MIN_TIME_IN_DAYS_BETWEEN_ALLOWED_RESETS'
        )
        return min_days_between_reset

    @classmethod
    def is_staff_forced_password_reset_enabled(cls):
        """
        Returns whether the configuration which forces password resets to occur has been turned on
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return False
        min_days_between_reset = settings.ADVANCED_SECURITY_CONFIG.get(
            'MIN_DAYS_FOR_STAFF_ACCOUNTS_PASSWORD_RESETS'
        )
        return min_days_between_reset

    @classmethod
    def is_student_forced_password_reset_enabled(cls):
        """
        Returns whether the configuration which forces password resets to occur has been turned on
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return False
        min_days_pw_reset = settings.ADVANCED_SECURITY_CONFIG.get(
            'MIN_DAYS_FOR_STUDENT_ACCOUNTS_PASSWORD_RESETS'
        )
        return min_days_pw_reset

    @classmethod
    def should_user_reset_password_now(cls, user):
        """
        Returns whether a password has 'expired' and should be reset. Note there are two different
        expiry policies for staff and students
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return False

        days_before_password_reset = None
        if user.is_staff:
            if cls.is_staff_forced_password_reset_enabled():
                days_before_password_reset = \
                    settings.ADVANCED_SECURITY_CONFIG['MIN_DAYS_FOR_STAFF_ACCOUNTS_PASSWORD_RESETS']
        elif cls.is_student_forced_password_reset_enabled():
            days_before_password_reset = \
                settings.ADVANCED_SECURITY_CONFIG['MIN_DAYS_FOR_STUDENT_ACCOUNTS_PASSWORD_RESETS']

        if days_before_password_reset:
            history = PasswordHistory.objects.filter(user=user).order_by('-time_set')
            time_last_reset = None

            if history:
                # first element should be the last time we reset password
                time_last_reset = history[0].time_set
            else:
                # no history, then let's take the date the user joined
                time_last_reset = user.date_joined

            now = timezone.now()

            delta = now - time_last_reset

            return delta.days >= days_before_password_reset

        return False

    @classmethod
    def is_password_reset_too_soon(cls, user):
        """
        Verifies that the password is not getting reset too frequently
        """
        if not cls.is_password_reset_frequency_restricted():
            return False

        history = PasswordHistory.objects.filter(user=user).order_by('-time_set')

        if not history:
            return False

        now = timezone.now()

        delta = now - history[0].time_set

        return delta.days < settings.ADVANCED_SECURITY_CONFIG['MIN_TIME_IN_DAYS_BETWEEN_ALLOWED_RESETS']

    @classmethod
    def is_allowable_password_reuse(cls, user, new_password):
        """
        Verifies that the password adheres to the reuse policies
        """
        if not settings.FEATURES['ADVANCED_SECURITY']:
            return True

        if user.is_staff and cls.is_staff_password_reuse_restricted():
            min_diff_passwords_required = \
                settings.ADVANCED_SECURITY_CONFIG['MIN_DIFFERENT_STAFF_PASSWORDS_BEFORE_REUSE']
        elif cls.is_student_password_reuse_restricted():
            min_diff_passwords_required = \
                settings.ADVANCED_SECURITY_CONFIG['MIN_DIFFERENT_STUDENT_PASSWORDS_BEFORE_REUSE']
        else:
            min_diff_passwords_required = 0

        # just limit the result set to the number of different
        # password we need
        history = PasswordHistory.objects.filter(user=user).order_by('-time_set')[:min_diff_passwords_required]

        for entry in history:

            # be sure to re-use the same salt
            # NOTE, how the salt is serialized in the password field is dependent on the algorithm
            # in pbkdf2_sha256 [LMS] it's the 3rd element, in sha1 [unit tests] it's the 2nd element
            hash_elements = entry.password.split('$')
            algorithm = hash_elements[0]
            if algorithm == 'pbkdf2_sha256':
                hashed_password = make_password(new_password, hash_elements[2])
            elif algorithm == 'sha1':
                hashed_password = make_password(new_password, hash_elements[1])
            else:
                # This means we got something unexpected. We don't want to throw an exception, but
                # log as an error and basically allow any password reuse
                AUDIT_LOG.error('''
                                Unknown password hashing algorithm "{0}" found in existing password
                                hash, password reuse policy will not be enforced!!!
                                '''.format(algorithm))
                return True

            if entry.password == hashed_password:
                return False

        return True


class LoginFailures(models.Model):
    """
    This model will keep track of failed login attempts
    """
    user = models.ForeignKey(User)
    failure_count = models.IntegerField(default=0)
    lockout_until = models.DateTimeField(null=True)

    @classmethod
    def is_feature_enabled(cls):
        """
        Returns whether the feature flag around this functionality has been set
        """
        return settings.FEATURES['ENABLE_MAX_FAILED_LOGIN_ATTEMPTS']

    @classmethod
    def is_user_locked_out(cls, user):
        """
        Static method to return in a given user has his/her account locked out
        """
        try:
            record = LoginFailures.objects.get(user=user)
            if not record.lockout_until:
                return False

            now = datetime.now(UTC)
            until = record.lockout_until
            is_locked_out = until and now < until

            return is_locked_out
        except ObjectDoesNotExist:
            return False

    @classmethod
    def increment_lockout_counter(cls, user):
        """
        Ticks the failed attempt counter
        """
        record, _ = LoginFailures.objects.get_or_create(user=user)
        record.failure_count = record.failure_count + 1
        max_failures_allowed = settings.MAX_FAILED_LOGIN_ATTEMPTS_ALLOWED

        # did we go over the limit in attempts
        if record.failure_count >= max_failures_allowed:
            # yes, then store when this account is locked out until
            lockout_period_secs = settings.MAX_FAILED_LOGIN_ATTEMPTS_LOCKOUT_PERIOD_SECS
            record.lockout_until = datetime.now(UTC) + timedelta(seconds=lockout_period_secs)

        record.save()

    @classmethod
    def clear_lockout_counter(cls, user):
        """
        Removes the lockout counters (normally called after a successful login)
        """
        try:
            entry = LoginFailures.objects.get(user=user)
            entry.delete()
        except ObjectDoesNotExist:
            return


class CourseEnrollmentException(Exception):
    pass


class NonExistentCourseError(CourseEnrollmentException):
    pass


class EnrollmentClosedError(CourseEnrollmentException):
    pass


class CourseFullError(CourseEnrollmentException):
    pass


class AlreadyEnrolledError(CourseEnrollmentException):
    pass


class CourseEnrollment(models.Model):
    """
    Represents a Student's Enrollment record for a single Course. You should
    generally not manipulate CourseEnrollment objects directly, but use the
    classmethods provided to enroll, unenroll, or check on the enrollment status
    of a given student.

    We're starting to consolidate course enrollment logic in this class, but
    more should be brought in (such as checking against CourseEnrollmentAllowed,
    checking course dates, user permissions, etc.) This logic is currently
    scattered across our views.
    """
    MODEL_TAGS = ['course_id', 'is_active', 'mode']

    user = models.ForeignKey(User)
    course_id = CourseKeyField(max_length=255, db_index=True)
    created = models.DateTimeField(auto_now_add=True, null=True, db_index=True)

    # If is_active is False, then the student is not considered to be enrolled
    # in the course (is_enrolled() will return False)
    is_active = models.BooleanField(default=True)

    # Represents the modes that are possible. We'll update this later with a
    # list of possible values.
    mode = models.CharField(default="honor", max_length=100)

    class Meta:
        unique_together = (('user', 'course_id'),)
        ordering = ('user', 'course_id')

    def __unicode__(self):
        return (
            "[CourseEnrollment] {}: {} ({}); active: ({})"
        ).format(self.user, self.course_id, self.created, self.is_active)

    @classmethod
    def get_or_create_enrollment(cls, user, course_key):
        """
        Create an enrollment for a user in a class. By default *this enrollment
        is not active*. This is useful for when an enrollment needs to go
        through some sort of approval process before being activated. If you
        don't need this functionality, just call `enroll()` instead.

        Returns a CoursewareEnrollment object.

        `user` is a Django User object. If it hasn't been saved yet (no `.id`
               attribute), this method will automatically save it before
               adding an enrollment for it.

        `course_id` is our usual course_id string (e.g. "edX/Test101/2013_Fall)

        It is expected that this method is called from a method which has already
        verified the user authentication and access.
        """
        # If we're passing in a newly constructed (i.e. not yet persisted) User,
        # save it to the database so that it can have an ID that we can throw
        # into our CourseEnrollment object. Otherwise, we'll get an
        # IntegrityError for having a null user_id.
        assert(isinstance(course_key, CourseKey))

        if user.id is None:
            user.save()

        enrollment, created = CourseEnrollment.objects.get_or_create(
            user=user,
            course_id=course_key,
        )

        # If we *did* just create a new enrollment, set some defaults
        if created:
            enrollment.mode = "honor"
            enrollment.is_active = False
            enrollment.save()

        return enrollment

    @classmethod
    def num_enrolled_in(cls, course_id):
        """
        Returns the count of active enrollments in a course.

        'course_id' is the course_id to return enrollments
        """
        enrollment_number = CourseEnrollment.objects.filter(course_id=course_id, is_active=1).count()

        return enrollment_number

    @classmethod
    def is_enrollment_closed(cls, user, course):
        """
        Returns a boolean value regarding whether the user has access to enroll in the course. Returns False if the
        enrollment has been closed.
        """
        # Disable the pylint error here, as per ormsbee. This local import was previously
        # in CourseEnrollment.enroll
        from courseware.access import has_access  # pylint: disable=import-error
        return not has_access(user, 'enroll', course)

    @classmethod
    def is_course_full(cls, course):
        """
        Returns a boolean value regarding whether a course has already reached it's max enrollment
        capacity
        """
        is_course_full = False
        if course.max_student_enrollments_allowed is not None:
            is_course_full = cls.num_enrolled_in(course.id) >= course.max_student_enrollments_allowed
        return is_course_full

    def update_enrollment(self, mode=None, is_active=None, skip_refund=False):
        """
        Updates an enrollment for a user in a class.  This includes options
        like changing the mode, toggling is_active True/False, etc.

        Also emits relevant events for analytics purposes.

        This saves immediately.

        """
        activation_changed = False
        # if is_active is None, then the call to update_enrollment didn't specify
        # any value, so just leave is_active as it is
        if self.is_active != is_active and is_active is not None:
            self.is_active = is_active
            activation_changed = True

        mode_changed = False
        # if mode is None, the call to update_enrollment didn't specify a new
        # mode, so leave as-is
        if self.mode != mode and mode is not None:
            self.mode = mode
            mode_changed = True

        if activation_changed or mode_changed:
            self.save()

        if activation_changed:
            if self.is_active:
                self.emit_event(EVENT_NAME_ENROLLMENT_ACTIVATED)

                dog_stats_api.increment(
                    "common.student.enrollment",
                    tags=[u"org:{}".format(self.course_id.org),
                          u"offering:{}".format(self.course_id.offering),
                          u"mode:{}".format(self.mode)]
                )

            else:
                UNENROLL_DONE.send(sender=None, course_enrollment=self, skip_refund=skip_refund)

                self.emit_event(EVENT_NAME_ENROLLMENT_DEACTIVATED)

                dog_stats_api.increment(
                    "common.student.unenrollment",
                    tags=[u"org:{}".format(self.course_id.org),
                          u"offering:{}".format(self.course_id.offering),
                          u"mode:{}".format(self.mode)]
                )
        if mode_changed:
            # the user's default mode is "honor" and disabled for a course
            # mode change events will only be emitted when the user's mode changes from this
            self.emit_event(EVENT_NAME_ENROLLMENT_MODE_CHANGED)

    def emit_event(self, event_name):
        """
        Emits an event to explicitly track course enrollment and unenrollment.
        """

        try:
            context = contexts.course_context_from_course_id(self.course_id)
            assert(isinstance(self.course_id, CourseKey))
            data = {
                'user_id': self.user.id,
                'course_id': self.course_id.to_deprecated_string(),
                'mode': self.mode,
            }

            with tracker.get_tracker().context(event_name, context):
                tracker.emit(event_name, data)

                if settings.FEATURES.get('SEGMENT_IO_LMS') and settings.SEGMENT_IO_LMS_KEY:
                    tracking_context = tracker.get_tracker().resolve_context()
                    analytics.track(self.user_id, event_name, {
                        'category': 'conversion',
                        'label': self.course_id.to_deprecated_string(),
                        'org': self.course_id.org,
                        'course': self.course_id.course,
                        'run': self.course_id.run,
                        'mode': self.mode,
                    }, context={
                        'Google Analytics': {
                            'clientId': tracking_context.get('client_id')
                        }
                    })

        except:  # pylint: disable=bare-except
            if event_name and self.course_id:
                log.exception('Unable to emit event %s for user %s and course %s', event_name, self.user.username, self.course_id)

    @classmethod
    def enroll(cls, user, course_key, mode="honor", check_access=False):
        """
        Enroll a user in a course. This saves immediately.

        Returns a CoursewareEnrollment object.

        `user` is a Django User object. If it hasn't been saved yet (no `.id`
               attribute), this method will automatically save it before
               adding an enrollment for it.

        `course_key` is our usual course_id string (e.g. "edX/Test101/2013_Fall)

        `mode` is a string specifying what kind of enrollment this is. The
               default is "honor", meaning honor certificate. Future options
               may include "audit", "verified_id", etc. Please don't use it
               until we have these mapped out.

        `check_access`: if True, we check that an accessible course actually
                exists for the given course_key before we enroll the student.
                The default is set to False to avoid breaking legacy code or
                code with non-standard flows (ex. beta tester invitations), but
                for any standard enrollment flow you probably want this to be True.

        Exceptions that can be raised: NonExistentCourseError,
        EnrollmentClosedError, CourseFullError, AlreadyEnrolledError.  All these
        are subclasses of CourseEnrollmentException if you want to catch all of
        them in the same way.

        It is expected that this method is called from a method which has already
        verified the user authentication.

        Also emits relevant events for analytics purposes.
        """
        # All the server-side checks for whether a user is allowed to enroll.
        try:
            course = modulestore().get_course(course_key)
        except ItemNotFoundError:
            log.warning(
                "User {0} failed to enroll in non-existent course {1}".format(
                    user.username,
                    course_key.to_deprecated_string()
                )
            )
            raise NonExistentCourseError

        if check_access:
            if course is None:
                raise NonExistentCourseError
            if CourseEnrollment.is_enrollment_closed(user, course):
                log.warning(
                    "User {0} failed to enroll in course {1} because enrollment is closed".format(
                        user.username,
                        course_key.to_deprecated_string()
                    )
                )
                raise EnrollmentClosedError

            if CourseEnrollment.is_course_full(course):
                log.warning(
                    "User {0} failed to enroll in full course {1}".format(
                        user.username,
                        course_key.to_deprecated_string()
                    )
                )
                raise CourseFullError
        if CourseEnrollment.is_enrolled(user, course_key):
            log.warning(
                "User {0} attempted to enroll in {1}, but they were already enrolled".format(
                    user.username,
                    course_key.to_deprecated_string()
                )
            )
            if check_access:
                raise AlreadyEnrolledError

        # User is allowed to enroll if they've reached this point.
        enrollment = cls.get_or_create_enrollment(user, course_key)
        enrollment.update_enrollment(is_active=True, mode=mode)
        return enrollment

    @classmethod
    def enroll_by_email(cls, email, course_id, mode="honor", ignore_errors=True):
        """
        Enroll a user in a course given their email. This saves immediately.

        Note that  enrolling by email is generally done in big batches and the
        error rate is high. For that reason, we supress User lookup errors by
        default.

        Returns a CoursewareEnrollment object. If the User does not exist and
        `ignore_errors` is set to `True`, it will return None.

        `email` Email address of the User to add to enroll in the course.

        `course_id` is our usual course_id string (e.g. "edX/Test101/2013_Fall)

        `mode` is a string specifying what kind of enrollment this is. The
               default is "honor", meaning honor certificate. Future options
               may include "audit", "verified_id", etc. Please don't use it
               until we have these mapped out.

        `ignore_errors` is a boolean indicating whether we should suppress
                        `User.DoesNotExist` errors (returning None) or let it
                        bubble up.

        It is expected that this method is called from a method which has already
        verified the user authentication and access.
        """
        try:
            user = User.objects.get(email=email)
            return cls.enroll(user, course_id, mode)
        except User.DoesNotExist:
            err_msg = u"Tried to enroll email {} into course {}, but user not found"
            log.error(err_msg.format(email, course_id))
            if ignore_errors:
                return None
            raise

    @classmethod
    def unenroll(cls, user, course_id, skip_refund=False):
        """
        Remove the user from a given course. If the relevant `CourseEnrollment`
        object doesn't exist, we log an error but don't throw an exception.

        `user` is a Django User object. If it hasn't been saved yet (no `.id`
               attribute), this method will automatically save it before
               adding an enrollment for it.

        `course_id` is our usual course_id string (e.g. "edX/Test101/2013_Fall)

        `skip_refund` can be set to True to avoid the refund process.
        """
        try:
            record = CourseEnrollment.objects.get(user=user, course_id=course_id)
            record.update_enrollment(is_active=False, skip_refund=skip_refund)

        except cls.DoesNotExist:
            err_msg = u"Tried to unenroll student {} from {} but they were not enrolled"
            log.error(err_msg.format(user, course_id))

    @classmethod
    def unenroll_by_email(cls, email, course_id):
        """
        Unenroll a user from a course given their email. This saves immediately.
        User lookup errors are logged but will not throw an exception.

        `email` Email address of the User to unenroll from the course.

        `course_id` is our usual course_id string (e.g. "edX/Test101/2013_Fall)
        """
        try:
            user = User.objects.get(email=email)
            return cls.unenroll(user, course_id)
        except User.DoesNotExist:
            err_msg = u"Tried to unenroll email {} from course {}, but user not found"
            log.error(err_msg.format(email, course_id))

    @classmethod
    def is_enrolled(cls, user, course_key):
        """
        Returns True if the user is enrolled in the course (the entry must exist
        and it must have `is_active=True`). Otherwise, returns False.

        `user` is a Django User object. If it hasn't been saved yet (no `.id`
               attribute), this method will automatically save it before
               adding an enrollment for it.

        `course_id` is our usual course_id string (e.g. "edX/Test101/2013_Fall)
        """
        try:
            record = CourseEnrollment.objects.get(user=user, course_id=course_key)
            return record.is_active
        except cls.DoesNotExist:
            return False

    @classmethod
    def is_enrolled_by_partial(cls, user, course_id_partial):
        """
        Returns `True` if the user is enrolled in a course that starts with
        `course_id_partial`. Otherwise, returns False.

        Can be used to determine whether a student is enrolled in a course
        whose run name is unknown.

        `user` is a Django User object. If it hasn't been saved yet (no `.id`
               attribute), this method will automatically save it before
               adding an enrollment for it.

        `course_id_partial` (CourseKey) is missing the run component
        """
        assert isinstance(course_id_partial, CourseKey)
        assert not course_id_partial.run  # None or empty string
        course_key = SlashSeparatedCourseKey(course_id_partial.org, course_id_partial.course, '')
        querystring = unicode(course_key.to_deprecated_string())
        try:
            return CourseEnrollment.objects.filter(
                user=user,
                course_id__startswith=querystring,
                is_active=1
            ).exists()
        except cls.DoesNotExist:
            return False

    @classmethod
    def enrollment_mode_for_user(cls, user, course_id):
        """
        Returns the enrollment mode for the given user for the given course

        `user` is a Django User object
        `course_id` is our usual course_id string (e.g. "edX/Test101/2013_Fall)

        Returns (mode, is_active) where mode is the enrollment mode of the student
            and is_active is whether the enrollment is active.
        Returns (None, None) if the courseenrollment record does not exist.
        """
        try:
            record = CourseEnrollment.objects.get(user=user, course_id=course_id)
            return (record.mode, record.is_active)
        except cls.DoesNotExist:
            return (None, None)

    @classmethod
    def enrollments_for_user(cls, user):
        return CourseEnrollment.objects.filter(user=user, is_active=1)

    @classmethod
    def users_enrolled_in(cls, course_id):
        """Return a queryset of User for every user enrolled in the course."""
        return User.objects.filter(
            courseenrollment__course_id=course_id,
            courseenrollment__is_active=True
        )

    @classmethod
    def enrollment_counts(cls, course_id):
        """
        Returns a dictionary that stores the total enrollment count for a course, as well as the
        enrollment count for each individual mode.
        """
        # Unfortunately, Django's "group by"-style queries look super-awkward
        query = use_read_replica_if_available(cls.objects.filter(course_id=course_id, is_active=True).values('mode').order_by().annotate(Count('mode')))
        total = 0
        enroll_dict = defaultdict(int)
        for item in query:
            enroll_dict[item['mode']] = item['mode__count']
            total += item['mode__count']
        enroll_dict['total'] = total
        return enroll_dict

    def is_paid_course(self):
        """
        Returns True, if course is paid
        """
        paid_course = CourseMode.objects.filter(Q(course_id=self.course_id) & Q(mode_slug='honor') &
                                                (Q(expiration_datetime__isnull=True) | Q(expiration_datetime__gte=datetime.now(pytz.UTC)))).exclude(min_price=0)
        if paid_course or self.mode == 'professional':
            return True

        return False

    def activate(self):
        """Makes this `CourseEnrollment` record active. Saves immediately."""
        self.update_enrollment(is_active=True)

    def deactivate(self):
        """Makes this `CourseEnrollment` record inactive. Saves immediately. An
        inactive record means that the student is not enrolled in this course.
        """
        self.update_enrollment(is_active=False)

    def change_mode(self, mode):
        """Changes this `CourseEnrollment` record's mode to `mode`.  Saves immediately."""
        self.update_enrollment(mode=mode)

    def refundable(self):
        """
        For paid/verified certificates, students may receive a refund if they have
        a verified certificate and the deadline for refunds has not yet passed.
        """
        # In order to support manual refunds past the deadline, set can_refund on this object.
        # On unenrolling, the "UNENROLL_DONE" signal calls CertificateItem.refund_cert_callback(),
        # which calls this method to determine whether to refund the order.
        # This can't be set directly because refunds currently happen as a side-effect of unenrolling.
        # (side-effects are bad)
        if getattr(self, 'can_refund', None) is not None:
            return True

        # If the student has already been given a certificate they should not be refunded
        if GeneratedCertificate.certificate_for_student(self.user, self.course_id) is not None:
            return False

        #TODO - When Course administrators to define a refund period for paid courses then refundable will be supported. # pylint: disable=fixme

        course_mode = CourseMode.mode_for_course(self.course_id, 'verified')
        if course_mode is None:
            return False
        else:
            return True

    @property
    def username(self):
        return self.user.username

    @property
    def course(self):
        return modulestore().get_course(self.course_id)


class CourseEnrollmentAllowed(models.Model):
    """
    Table of users (specified by email address strings) who are allowed to enroll in a specified course.
    The user may or may not (yet) exist.  Enrollment by users listed in this table is allowed
    even if the enrollment time window is past.
    """
    email = models.CharField(max_length=255, db_index=True)
    course_id = CourseKeyField(max_length=255, db_index=True)
    auto_enroll = models.BooleanField(default=0)

    created = models.DateTimeField(auto_now_add=True, null=True, db_index=True)

    class Meta:  # pylint: disable=missing-docstring
        unique_together = (('email', 'course_id'),)

    def __unicode__(self):
        return "[CourseEnrollmentAllowed] %s: %s (%s)" % (self.email, self.course_id, self.created)


@total_ordering
class CourseAccessRole(models.Model):
    """
    Maps users to org, courses, and roles. Used by student.roles.CourseRole and OrgRole.
    To establish a user as having a specific role over all courses in the org, create an entry
    without a course_id.
    """

    objects = NoneToEmptyManager()

    user = models.ForeignKey(User)
    # blank org is for global group based roles such as course creator (may be deprecated)
    org = models.CharField(max_length=64, db_index=True, blank=True)
    # blank course_id implies org wide role
    course_id = CourseKeyField(max_length=255, db_index=True, blank=True)
    role = models.CharField(max_length=64, db_index=True)

    class Meta:  # pylint: disable=missing-docstring
        unique_together = ('user', 'org', 'course_id', 'role')

    @property
    def _key(self):
        """
        convenience function to make eq overrides easier and clearer. arbitrary decision
        that role is primary, followed by org, course, and then user
        """
        return (self.role, self.org, self.course_id, self.user_id)

    def __eq__(self, other):
        """
        Overriding eq b/c the django impl relies on the primary key which requires fetch. sometimes we
        just want to compare roles w/o doing another fetch.
        """
        return type(self) == type(other) and self._key == other._key  # pylint: disable=protected-access

    def __hash__(self):
        return hash(self._key)

    def __lt__(self, other):
        """
        Lexigraphic sort
        """
        return self._key < other._key  # pylint: disable=protected-access

    def __unicode__(self):
        return "[CourseAccessRole] user: {}   role: {}   org: {}   course: {}".format(self.user.username, self.role, self.org, self.course_id)


class CourseAccessRoleAdmin(admin.ModelAdmin):
    raw_id_fields = ("user",)

#### Helper methods for use from python manage.py shell and other classes.


def get_user_by_username_or_email(username_or_email):
    """
    Return a User object, looking up by email if username_or_email contains a
    '@', otherwise by username.

    Raises:
        User.DoesNotExist is lookup fails.
    """
    if '@' in username_or_email:
        return User.objects.get(email=username_or_email)
    else:
        return User.objects.get(username=username_or_email)


def get_user(email):
    user = User.objects.get(email=email)
    u_prof = UserProfile.objects.get(user=user)
    return user, u_prof


def user_info(email):
    user, u_prof = get_user(email)
    print "User id", user.id
    print "Username", user.username
    print "E-mail", user.email
    print "Name", u_prof.name
    print "Location", u_prof.location
    print "Language", u_prof.language
    return user, u_prof


def change_email(old_email, new_email):
    user = User.objects.get(email=old_email)
    user.email = new_email
    user.save()


def change_name(email, new_name):
    _user, u_prof = get_user(email)
    u_prof.name = new_name
    u_prof.save()


def user_count():
    print "All users", User.objects.all().count()
    print "Active users", User.objects.filter(is_active=True).count()
    return User.objects.all().count()


def active_user_count():
    return User.objects.filter(is_active=True).count()


def create_group(name, description):
    utg = UserTestGroup()
    utg.name = name
    utg.description = description
    utg.save()


def add_user_to_group(user, group):
    utg = UserTestGroup.objects.get(name=group)
    utg.users.add(User.objects.get(username=user))
    utg.save()


def remove_user_from_group(user, group):
    utg = UserTestGroup.objects.get(name=group)
    utg.users.remove(User.objects.get(username=user))
    utg.save()

DEFAULT_GROUPS = {
    'email_future_courses': 'Receive e-mails about future MITx courses',
    'email_helpers': 'Receive e-mails about how to help with MITx',
    'mitx_unenroll': 'Fully unenrolled -- no further communications',
    '6002x_unenroll': 'Took and dropped 6002x'
}


def add_user_to_default_group(user, group):
    try:
        utg = UserTestGroup.objects.get(name=group)
    except UserTestGroup.DoesNotExist:
        utg = UserTestGroup()
        utg.name = group
        utg.description = DEFAULT_GROUPS[group]
        utg.save()
    utg.users.add(User.objects.get(username=user))
    utg.save()


def create_comments_service_user(user):
    if not settings.FEATURES['ENABLE_DISCUSSION_SERVICE']:
        # Don't try--it won't work, and it will fill the logs with lots of errors
        return
    try:
        cc_user = cc.User.from_django_user(user)
        cc_user.save()
    except Exception:  # pylint: disable=broad-except
        log = logging.getLogger("edx.discussion")  # pylint: disable=redefined-outer-name
        log.error(
            "Could not create comments service user with id {}".format(user.id),
            exc_info=True
        )

# Define login and logout handlers here in the models file, instead of the views file,
# so that they are more likely to be loaded when a Studio user brings up the Studio admin
# page to login.  These are currently the only signals available, so we need to continue
# identifying and logging failures separately (in views).


@receiver(user_logged_in)
def log_successful_login(sender, request, user, **kwargs):  # pylint: disable=unused-argument
    """Handler to log when logins have occurred successfully."""
    if settings.FEATURES['SQUELCH_PII_IN_LOGS']:
        AUDIT_LOG.info(u"Login success - user.id: {0}".format(user.id))
    else:
        AUDIT_LOG.info(u"Login success - {0} ({1})".format(user.username, user.email))


@receiver(user_logged_out)
def log_successful_logout(sender, request, user, **kwargs):  # pylint: disable=unused-argument
    """Handler to log when logouts have occurred successfully."""
    if settings.FEATURES['SQUELCH_PII_IN_LOGS']:
        AUDIT_LOG.info(u"Logout - user.id: {0}".format(request.user.id))
    else:
        AUDIT_LOG.info(u"Logout - {0}".format(request.user))


@receiver(user_logged_in)
@receiver(user_logged_out)
def enforce_single_login(sender, request, user, signal, **kwargs):    # pylint: disable=unused-argument
    """
    Sets the current session id in the user profile,
    to prevent concurrent logins.
    """
    if settings.FEATURES.get('PREVENT_CONCURRENT_LOGINS', False):
        if signal == user_logged_in:
            key = request.session.session_key
        else:
            key = None
        user.profile.set_login_session(key)


class DashboardConfiguration(ConfigurationModel):
    """Dashboard Configuration settings.

    Includes configuration options for the dashboard, which impact behavior and rendering for the application.

    """
    recent_enrollment_time_delta = models.PositiveIntegerField(
        default=0,
        help_text="The number of seconds in which a new enrollment is considered 'recent'. "
                  "Used to display notifications."
    )

    @property
    def recent_enrollment_seconds(self):
        return self.recent_enrollment_time_delta


### DEKKER BLBK


class LTIUserAuth(models.Model):
    """
    Keeps track of authenticated anon_user IDs with institution meta-data and their respective user
    """
    user = models.ForeignKey(User, db_index=True)
    roles = models.TextField(blank=False)
    institution = models.TextField(blank=True)
    lti_user_id = models.TextField(blank=False)
    lti_data = models.TextField(blank=False)
    lti_email = models.TextField(blank=True)

    def __unicode__(self):
        return "[LTIUserAuth] %s %s = %s" % (self.user, self.roles, self.lti_data)

### END DEKKER