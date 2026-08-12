"""
Django REST Framework extensions for LTI 1.3 & LTI Advantage implementation.

Implements a custom authentication class to be used by LTI Advantage extensions.
"""
import logging

from django.contrib.auth.models import AnonymousUser
from django.utils.translation import gettext as _
from rest_framework import authentication
from rest_framework import exceptions

from lti_consumer.models import LtiConfiguration

log = logging.getLogger(__name__)


class Lti1p3ApiAuthentication(authentication.BaseAuthentication):
    """
    LTI 1.3 Token based authentication.

    Clients should authenticate by passing the token key in the "Authorization".
    LTI 1.3 expects a token like the following:
        Authorization: Bearer jwt-token

    Since the base implementation of this library uses JWT tokens, we expect
    a RSA256 signed token that contains the allowed scopes.
    """
    keyword = 'Bearer'

    def authenticate(self, request):
        """
        Authenticate an LTI 1.3 Tool.

        This doesn't return a user, but let's the external access and commit
        changes.
        TODO: Consider creating an user for LTI operations, both to keep track
        of changes and to use Django's authorization flow.
        """
        auth = request.headers.get('Authorization', '').split()
        lti_config_id = request.parser_context['kwargs'].get('lti_config_id')

        # Check if auth token is present on request and is correctly formatted.
        if not auth or auth[0].lower() != self.keyword.lower():
            log.warning(
                'LTI 1.3 API authentication failed for lti_config_id=%s: missing or malformed Authorization header.',
                lti_config_id,
            )
            msg = _('Missing LTI 1.3 authentication token.')
            raise exceptions.AuthenticationFailed(msg)

        if len(auth) == 1:
            log.warning(
                'LTI 1.3 API authentication failed for lti_config_id=%s: Authorization header had no credentials.',
                lti_config_id,
            )
            msg = _('Invalid token header. No credentials provided.')
            raise exceptions.AuthenticationFailed(msg)

        if len(auth) > 2:
            log.warning(
                'LTI 1.3 API authentication failed for lti_config_id=%s: Authorization header contained spaces.',
                lti_config_id,
            )
            msg = _('Invalid token header. Token string should not contain spaces.')
            raise exceptions.AuthenticationFailed(msg)

        # Retrieve LTI configuration or fail if it doesn't exist
        try:
            lti_configuration = LtiConfiguration.objects.get(pk=lti_config_id)
            lti_consumer = lti_configuration.get_lti_consumer()
        except Exception as err:
            log.warning(
                'LTI 1.3 API authentication failed: could not load LtiConfiguration for lti_config_id=%s: %s',
                lti_config_id,
                err,
            )
            msg = _('LTI configuration not found.')
            raise exceptions.AuthenticationFailed(msg) from err

        # Verify token validity
        # This doesn't validate specific permissions, just checks if the token
        # is valid or not.
        try:
            lti_consumer.check_token(auth[1])
        except Exception as err:
            log.warning(
                'LTI 1.3 API authentication failed for lti_config_id=%s: access token could not be validated '
                '(%s: %s).',
                lti_config_id,
                type(err).__name__,
                err,
            )
            msg = _('Invalid token signature.')
            raise exceptions.AuthenticationFailed(msg) from err

        # Passing parameters back to the view through the request in order
        # to avoid implementing a separate authentication backend or
        # keeping track of LTI "sessions" through a custom model.
        # With the LTI Configuration and consumer attached to the request
        # the views and permissions classes can make use of the
        # current LTI context to retrieve settings and decode the token passed.
        request.lti_configuration = lti_configuration
        request.lti_consumer = lti_consumer

        # This isn't tied to any authentication backend on Django (it's just
        # used for LTI endpoints), but we need to return some kind of User
        # object or it will break edx-platform middleware that assumes it can
        # introspect a user object on the request (e.g. DarkLangMiddleware).
        return (AnonymousUser(), None)
