"""
Authentication service using LDAP bind against UPHS Active Directory.

Provides:
    - LDAP bind authentication (validates username/password)
    - AD group membership check (restricts access to IS teams)
    - Session token creation/validation (signed cookies)
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass

import ldap3
from ldap3 import Server, Connection, ALL, SUBTREE
from ldap3.core.exceptions import LDAPBindError, LDAPException

logger = logging.getLogger(__name__)


@dataclass
class AuthUser:
    """Authenticated user information."""
    username: str
    display_name: str
    groups: list[str]
    login_time: float


class AuthService:
    """LDAP-based authentication and authorization service."""

    def __init__(
        self,
        ldap_server: str,
        ldap_domain: str,
        allowed_groups: list[str],
        allowed_usernames: list[str],
        session_secret: str,
        session_expire_hours: float = 12.0,
    ):
        self.ldap_server = ldap_server
        self.ldap_domain = ldap_domain
        self.allowed_groups = [g.strip().lower() for g in allowed_groups]
        self.allowed_usernames = [u.strip().lower() for u in allowed_usernames]
        self.session_secret = session_secret
        self.session_expire_seconds = session_expire_hours * 3600

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        """
        Authenticate user via LDAP bind and check group membership.

        Returns AuthUser on success, None on failure.
        Raises ValueError with user-friendly message on authorization failure.
        """
        username = username.strip()
        if not username or not password:
            return None

        # Attempt LDAP bind using SIMPLE auth with UPN format
        # (NTLM requires MD4 which is removed in Python 3.14)
        upn = f"{username}@UPHS.PENNHEALTH.PRV"
        try:
            server = Server(self.ldap_server, get_info=ALL, connect_timeout=10)
            conn = Connection(
                server,
                user=upn,
                password=password,
                auto_bind=True,
                receive_timeout=10,
            )
        except LDAPBindError:
            logger.info("LDAP bind failed for user: %s", username)
            return None
        except LDAPException as e:
            logger.error("LDAP connection error: %s", e)
            raise ValueError("Unable to connect to authentication server. Please try again later.")

        # Get user info and group membership
        try:
            # Build base DN from domain
            base_dn = ",".join(f"DC={part}" for part in self.ldap_domain.split("."))
            # For UPHS.PENNHEALTH.PRV → DC=UPHS,DC=PENNHEALTH,DC=PRV
            # But since we use just "UPHS" as domain for NTLM, search the full forest
            search_base = "DC=UPHS,DC=PENNHEALTH,DC=PRV"

            conn.search(
                search_base=search_base,
                search_filter=f"(sAMAccountName={username})",
                search_scope=SUBTREE,
                attributes=["displayName", "memberOf", "cn"],
            )

            if not conn.entries:
                logger.warning("User %s authenticated but not found in directory search", username)
                # Fallback: allow if in allowed_usernames
                if username.lower() in self.allowed_usernames:
                    return AuthUser(
                        username=username,
                        display_name=username,
                        groups=[],
                        login_time=time.time(),
                    )
                raise ValueError("Account not found in directory. Contact your administrator.")

            entry = conn.entries[0]
            display_name = str(entry.displayName) if entry.displayName else username
            member_of = [str(g) for g in entry.memberOf] if entry.memberOf else []

        except LDAPException as e:
            logger.error("LDAP search error for %s: %s", username, e)
            # If search fails but bind succeeded, allow fallback users
            if username.lower() in self.allowed_usernames:
                return AuthUser(
                    username=username,
                    display_name=username,
                    groups=[],
                    login_time=time.time(),
                )
            raise ValueError("Directory search failed. Please try again later.")
        finally:
            conn.unbind()

        # Check authorization: user must be in allowed groups OR allowed usernames
        if username.lower() in self.allowed_usernames:
            logger.info("User %s authorized via allowed_usernames fallback", username)
            return AuthUser(
                username=username,
                display_name=display_name,
                groups=self._extract_group_names(member_of),
                login_time=time.time(),
            )

        user_groups = self._extract_group_names(member_of)
        if self._is_authorized(user_groups):
            logger.info("User %s authorized via group membership", username)
            return AuthUser(
                username=username,
                display_name=display_name,
                groups=user_groups,
                login_time=time.time(),
            )

        logger.warning("User %s authenticated but not authorized (groups: %s)", username, user_groups)
        raise ValueError(
            "Access denied. You are not a member of an authorized group. "
            "This application is restricted to IS Service Desk staff."
        )

    def _extract_group_names(self, member_of: list[str]) -> list[str]:
        """Extract CN (common name) from full DN strings."""
        groups = []
        for dn in member_of:
            for part in dn.split(","):
                if part.strip().upper().startswith("CN="):
                    groups.append(part.strip()[3:])
                    break
        return groups

    def _is_authorized(self, user_groups: list[str]) -> bool:
        """Check if any of the user's groups match allowed groups."""
        user_groups_lower = [g.lower() for g in user_groups]
        return any(allowed in user_groups_lower for allowed in self.allowed_groups)

    # ── Session Token Management ───────────────────────────────────────

    def create_session_token(self, user: AuthUser) -> str:
        """Create a signed session token containing user info."""
        payload = {
            "username": user.username,
            "display_name": user.display_name,
            "login_time": user.login_time,
        }
        data = json.dumps(payload, separators=(",", ":"))
        signature = self._sign(data)
        return f"{data}|{signature}"

    def validate_session_token(self, token: str) -> AuthUser | None:
        """Validate a session token and return the user if valid."""
        if not token or "|" not in token:
            return None

        try:
            data, signature = token.rsplit("|", 1)
            expected_sig = self._sign(data)
            if not hmac.compare_digest(signature, expected_sig):
                return None

            payload = json.loads(data)
            login_time = payload.get("login_time", 0)

            # Check expiry
            if time.time() - login_time > self.session_expire_seconds:
                return None

            return AuthUser(
                username=payload["username"],
                display_name=payload["display_name"],
                groups=[],
                login_time=login_time,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _sign(self, data: str) -> str:
        """Create HMAC-SHA256 signature."""
        return hmac.HMAC(
            self.session_secret.encode(),
            data.encode(),
            hashlib.sha256,
        ).hexdigest()
