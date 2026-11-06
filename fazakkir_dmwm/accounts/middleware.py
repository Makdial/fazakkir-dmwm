"""Give every visitor a guest identity without asking them to sign in."""
from __future__ import annotations

from django.contrib.auth import logout

from .guests import is_guest_user, login_guest

# On these prefixes a guest must step aside so an administrator can sign in.
# No guest account is created here.
_LOGOUT_PREFIXES = (
    "/accounts/",
    "/admin/",
    "/manage/",
)

# These never need an account and must not touch the visitor's session: creating
# or flushing one here would rotate their CSRF cookie and break the next save
# (the service worker checks /sw.js on every page load, for example).
_NO_GUEST_PATHS = {
    "/healthz/",
    "/manifest.webmanifest",
    "/sw.js",
    "/favicon.ico",
    "/offline/",
}
_NO_GUEST_PREFIXES = (
    "/static/",
    "/media/",
    "/i18n/",
)


def _needs_logout(path: str) -> bool:
    return path.startswith(_LOGOUT_PREFIXES)


def _no_guest(path: str) -> bool:
    return path in _NO_GUEST_PATHS or path.startswith(_NO_GUEST_PREFIXES)


class GuestAccessMiddleware:
    """Sign anonymous visitors in as a throw-away guest account.

    This runs after ``AuthenticationMiddleware`` so ``request.user`` is already
    available. Existing real accounts are left untouched.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path_info

        if _no_guest(path):
            response = self.get_response(request)
            # An empty session would make SessionMiddleware delete the visitor's
            # session cookie on the way out. Keep it untouched.
            session = getattr(request, "session", None)
            if session is not None:
                session.accessed = False
                session.modified = False
            return response

        if _needs_logout(path):
            # On the account/admin pages a guest must step aside so the login
            # form is reachable (Django redirects already-authenticated users
            # away from the login page).
            if is_guest_user(getattr(request, "user", None)):
                logout(request)
            return self.get_response(request)

        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            login_guest(request)

        return self.get_response(request)
