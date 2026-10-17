"""Google connectors (Gmail, Calendar, Drive, YouTube) over the shared interface.

Read operations run without approval; every write/external operation requires a
per-run human approval and idempotency protection. Least-privilege scopes:
Drive defaults to ``drive.file`` (app-created files only) — the broader
``drive.readonly`` listing scope is declared on the operation and reported as
``missing_scope`` until the user explicitly requests it during authorization.
"""
from __future__ import annotations

import base64
import contextlib
import json
import secrets
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

import httpx

from .base import (Connector, ConnectorBlocked, ConnectorError, ConnectorRateLimited,
                   ConnectorSpec, Field, Operation, scrub_secrets)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

GMAIL_READ = ("https://www.googleapis.com/auth/gmail.readonly",)
GMAIL_SEND = ("https://www.googleapis.com/auth/gmail.send",)
CALENDAR_READ = ("https://www.googleapis.com/auth/calendar.readonly",)
CALENDAR_WRITE = ("https://www.googleapis.com/auth/calendar.events",)
DRIVE_READ = ("https://www.googleapis.com/auth/drive.readonly",)
DRIVE_FILE = ("https://www.googleapis.com/auth/drive.file",)
YOUTUBE_READ = ("https://www.googleapis.com/auth/youtube.readonly",)


def authorization_url(client: dict, scopes: list[str], state: str, redirect_uri: str) -> str:
    params = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state}
    return GOOGLE_AUTH_URL + "?" + urlencode(params)


class GoogleConnector(Connector):
    """Shared Google HTTP plumbing: bearer requests, refresh, honest errors."""

    base_url = ""

    async def _http(self, method: str, url: str, *, params=None, data=None, json_body=None,
                    headers=None, content=None, timeout: float = 20.0):
        async with httpx.AsyncClient(transport=self.transport, timeout=timeout) as client:
            try:
                return await client.request(method, url, params=params, data=data, json=json_body,
                                            headers=headers, content=content)
            except httpx.HTTPError as exc:
                error = ConnectorError(scrub_secrets(f"network error: {exc}"))
                error.retryable = True
                raise error from exc

    async def valid_token(self) -> dict:
        token = self.store.token(self.spec.id)
        if not token:
            raise ConnectorBlocked("blocked_missing_credentials",
                                   "OAuth authorization has not been completed for this connector")
        if float(token.get("expires_at") or 0) - 60 <= time.time():
            refreshed = await self._refresh(token)
            if not refreshed:
                raise ConnectorBlocked("expired", "Access token expired and refresh failed; re-authorize this connector")
            token = refreshed
        if not token.get("access_token"):
            raise ConnectorBlocked("blocked_missing_credentials", "Stored credentials are incomplete")
        return token

    async def _refresh(self, token: dict) -> dict | None:
        if not token.get("refresh_token"):
            return None
        client = self.store.client_config(self.spec.id)
        if not client:
            return None
        payload = {"client_id": client["client_id"], "grant_type": "refresh_token",
                   "refresh_token": token["refresh_token"]}
        if client.get("client_secret"):
            payload["client_secret"] = client["client_secret"]
        try:
            response = await self._http("POST", GOOGLE_TOKEN_URL, data=payload, timeout=15)
        except ConnectorError:
            return None
        if response.status_code != 200:
            return None
        body = response.json() if response.content else {}
        if not isinstance(body, dict) or not body.get("access_token"):
            return None
        merged = dict(token)
        merged["access_token"] = body["access_token"]
        merged["expires_at"] = time.time() + float(body.get("expires_in", 3600)) - 30
        if body.get("scope"):
            merged["scopes"] = str(body["scope"]).split()
        self.store.save_token(self.spec.id, merged)
        return merged

    @staticmethod
    def _error_detail(response) -> str:
        try:
            body = response.json() or {}
        except ValueError:
            body = {}
        message = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
        elif isinstance(error, str):
            message = body.get("error_description") or error
        return scrub_secrets(message or response.text or f"HTTP {response.status_code}")

    def _check(self, response):
        if response.status_code >= 400:
            detail = self._error_detail(response)
            if response.status_code == 401:
                error = ConnectorBlocked("expired", f"Google rejected the access token ({detail})")
                error.http_status = 401
                raise error
            if response.status_code == 403 and any(word in detail.lower() for word in ("insufficient", "scope", "permission")):
                error = ConnectorBlocked("missing_scope", f"Google denied access: {detail}")
                error.http_status = 403
                raise error
            if response.status_code == 429:
                retry_after = 30.0
                with contextlib.suppress(TypeError, ValueError):
                    retry_after = float(response.headers.get("Retry-After", 30))
                error = ConnectorRateLimited(f"Google rate limit: {detail}", retry_after=retry_after)
                error.http_status = 429
                raise error
            error = ConnectorError(f"Google API error {response.status_code}: {detail}")
            error.http_status = response.status_code
            error.retryable = response.status_code >= 500
            raise error
        return response

    async def _request(self, method: str, path: str, access_token: str, *, params=None,
                       json_body=None, content=None, extra_headers=None):
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        response = await self._http(method, self.base_url + path, params=params, json_body=json_body,
                                    content=content, headers=headers, timeout=self._timeout())
        return self._check(response)

    def _timeout(self) -> float:
        return max(operation.timeout_seconds for operation in self.spec.operations) if self.spec.operations else 20.0

    async def execute(self, operation: Operation, args: dict, access_token: str) -> dict:
        return await self._call(operation, args, access_token)

    async def _call(self, operation: Operation, args: dict, access_token: str) -> dict:
        raise NotImplementedError

    @staticmethod
    def _json(response) -> dict:
        try:
            body = response.json()
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}


class GmailConnector(GoogleConnector):
    base_url = "https://gmail.googleapis.com/gmail/v1"

    async def _call(self, operation: Operation, args: dict, access_token: str) -> dict:
        if operation.name == "messages.search":
            params = {"q": args["query"], "maxResults": min(int(args.get("max_results", 10)), 20)}
            body = self._json(await self._request("GET", "/users/me/messages", access_token, params=params))
            messages = [{"id": item.get("id"), "thread_id": item.get("threadId")}
                        for item in body.get("messages", []) or []]
            return {"data": {"messages": messages, "count": len(messages)}, "result_ref": None}
        if operation.name == "messages.get":
            params = {"format": "metadata"}
            body = self._json(await self._request("GET", f"/users/me/messages/{args['message_id']}",
                                                  access_token, params=params))
            headers = {str(header.get("name", "")).lower(): header.get("value")
                       for header in (body.get("payload", {}) or {}).get("headers", []) or []}
            return {"data": {"id": body.get("id"), "thread_id": body.get("threadId"),
                             "snippet": body.get("snippet"), "subject": headers.get("subject"),
                             "from": headers.get("from"), "date": headers.get("date"),
                             "label_ids": body.get("labelIds", [])},
                    "result_ref": body.get("id")}
        if operation.name == "messages.send":
            message = EmailMessage()
            message["To"] = ", ".join(args["to"])
            message["Subject"] = args["subject"]
            message.set_content(args["body"])
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
            body = self._json(await self._request("POST", "/users/me/messages/send", access_token,
                                                  json_body={"raw": raw}))
            return {"data": {"id": body.get("id"), "thread_id": body.get("threadId"),
                             "to": args["to"], "subject": args["subject"]},
                    "result_ref": body.get("id")}
        raise ConnectorError(f"Unsupported operation: {operation.name}")


class CalendarConnector(GoogleConnector):
    base_url = "https://www.googleapis.com/calendar/v3"

    async def _call(self, operation: Operation, args: dict, access_token: str) -> dict:
        if operation.name == "events.list":
            params = {"singleEvents": "true", "orderBy": "startTime",
                      "maxResults": min(int(args.get("max_results", 25)), 50),
                      "timeMin": args.get("time_min") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
            if args.get("time_max"):
                params["timeMax"] = args["time_max"]
            body = self._json(await self._request("GET", "/calendars/primary/events", access_token, params=params))
            events = [{"id": item.get("id"), "summary": item.get("summary"),
                       "start": (item.get("start") or {}).get("dateTime") or (item.get("start") or {}).get("date"),
                       "end": (item.get("end") or {}).get("dateTime") or (item.get("end") or {}).get("date"),
                       "status": item.get("status")}
                      for item in body.get("items", []) or []]
            return {"data": {"events": events, "count": len(events)}, "result_ref": None}
        if operation.name == "events.create":
            payload = {"summary": args["summary"],
                       "start": {"dateTime": args["start"]},
                       "end": {"dateTime": args["end"]}}
            if args.get("description"):
                payload["description"] = args["description"]
            if args.get("location"):
                payload["location"] = args["location"]
            body = self._json(await self._request("POST", "/calendars/primary/events", access_token,
                                                  json_body=payload))
            return {"data": {"id": body.get("id"), "summary": body.get("summary"),
                             "html_link": body.get("htmlLink"), "status": body.get("status")},
                    "result_ref": body.get("id")}
        raise ConnectorError(f"Unsupported operation: {operation.name}")


class DriveConnector(GoogleConnector):
    base_url = "https://www.googleapis.com/drive/v3"

    async def _call(self, operation: Operation, args: dict, access_token: str) -> dict:
        if operation.name == "files.list":
            params = {"pageSize": min(int(args.get("page_size", 20)), 50), "fields": "files(id,name,mimeType,modifiedTime)"}
            if args.get("query"):
                params["q"] = args["query"]
            body = self._json(await self._request("GET", "/files", access_token, params=params))
            files = [{"id": item.get("id"), "name": item.get("name"),
                      "mime_type": item.get("mimeType"), "modified": item.get("modifiedTime")}
                     for item in body.get("files", []) or []]
            return {"data": {"files": files, "count": len(files)}, "result_ref": None}
        if operation.name == "files.upload":
            mime_type = args.get("mime_type") or "text/markdown"
            boundary = "cortexos-" + secrets.token_hex(8)
            multipart = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps({'name': args['name'], 'mimeType': mime_type})}\r\n"
                f"--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n{args['content']}\r\n--{boundary}--"
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {access_token}",
                       "Content-Type": f"multipart/related; boundary={boundary}"}
            response = await self._http("POST", "https://www.googleapis.com/upload/drive/v3/files",
                                        params={"uploadType": "multipart", "fields": "id,name,mimeType,webViewLink"},
                                        content=multipart, headers=headers, timeout=self._timeout())
            body = self._json(self._check(response))
            return {"data": {"id": body.get("id"), "name": body.get("name"),
                             "mime_type": body.get("mimeType"), "web_view_link": body.get("webViewLink")},
                    "result_ref": body.get("id")}
        raise ConnectorError(f"Unsupported operation: {operation.name}")


class YouTubeConnector(GoogleConnector):
    base_url = "https://www.googleapis.com/youtube/v3"

    async def _call(self, operation: Operation, args: dict, access_token: str) -> dict:
        if operation.name == "videos.search":
            params = {"part": "snippet", "type": "video", "q": args["query"],
                      "maxResults": min(int(args.get("max_results", 10)), 25)}
            body = self._json(await self._request("GET", "/search", access_token, params=params))
            videos = [{"video_id": (item.get("id") or {}).get("videoId"),
                       "title": (item.get("snippet") or {}).get("title"),
                       "channel": (item.get("snippet") or {}).get("channelTitle"),
                       "published_at": (item.get("snippet") or {}).get("publishedAt")}
                      for item in body.get("items", []) or []]
            return {"data": {"videos": videos, "count": len(videos)}, "result_ref": None}
        raise ConnectorError(f"Unsupported operation: {operation.name}")


def gmail_spec() -> ConnectorSpec:
    return ConnectorSpec(
        id="gmail", name="Gmail", version="1.0.0", auth="oauth2",
        description="Search and read Gmail messages; sending requires per-run human approval.",
        scopes=GMAIL_READ + GMAIL_SEND,
        setup=("Create an OAuth client (Desktop app) in Google Cloud, enable the Gmail API, then place "
               "{\"client_id\", \"client_secret\"} in config/connectors/gmail.json (chmod 600), then run "
               "`cortexos connectors authorize gmail`."),
        operations=(
            Operation("messages.search", "List message ids matching a Gmail query (read-only)", "read",
                      GMAIL_READ, approval=False, idempotency="none", retry_safe=True, max_retries=2,
                      fields={"query": Field("str", required=True, max_length=500),
                              "max_results": Field("int")}),
            Operation("messages.get", "Fetch message metadata (headers only) by message id (read-only)", "read",
                      GMAIL_READ, approval=False, idempotency="none", retry_safe=True, max_retries=2,
                      fields={"message_id": Field("str", required=True, max_length=200)}),
            Operation("messages.send", "Send a plain-text email (requires per-run human approval)", "external",
                      GMAIL_SEND, approval=True, idempotency="content_hash", retry_safe=False,
                      rate_limit_per_minute=20, timeout_seconds=30.0,
                      fields={"to": Field("str_list", required=True, max_items=20),
                              "subject": Field("str", required=True, max_length=200),
                              "body": Field("str", required=True, max_length=20000)}),
        ))


def calendar_spec() -> ConnectorSpec:
    return ConnectorSpec(
        id="calendar", name="Google Calendar", version="1.0.0", auth="oauth2",
        description="List upcoming events; creating events requires per-run human approval.",
        scopes=CALENDAR_READ + CALENDAR_WRITE,
        setup=("Enable the Google Calendar API for the same OAuth client, place its credentials in "
               "config/connectors/calendar.json (chmod 600), then run "
               "`cortexos connectors authorize calendar`."),
        operations=(
            Operation("events.list", "List upcoming events on the primary calendar (read-only)", "read",
                      CALENDAR_READ, approval=False, idempotency="none", retry_safe=True, max_retries=2,
                      fields={"time_min": Field("str", max_length=64), "time_max": Field("str", max_length=64),
                              "max_results": Field("int")}),
            Operation("events.create", "Create an event on the primary calendar (requires approval)", "external",
                      CALENDAR_WRITE, approval=True, idempotency="content_hash", retry_safe=False,
                      rate_limit_per_minute=20, timeout_seconds=30.0,
                      fields={"summary": Field("str", required=True, max_length=200),
                              "start": Field("str", required=True, max_length=64),
                              "end": Field("str", required=True, max_length=64),
                              "description": Field("str", max_length=2000),
                              "location": Field("str", max_length=300)}),
        ))


def drive_spec() -> ConnectorSpec:
    return ConnectorSpec(
        id="drive", name="Google Drive", version="1.0.0", auth="oauth2",
        description=("Work with app-created Drive files (least privilege). Listing all files needs the broader "
                     "drive.readonly scope and stays unavailable until explicitly granted."),
        scopes=DRIVE_FILE,
        setup=("Enable the Google Drive API for the same OAuth client, place its credentials in "
               "config/connectors/drive.json (chmod 600), then run `cortexos connectors authorize drive`."),
        operations=(
            Operation("files.list", "List Drive files (requires the optional drive.readonly scope)", "read",
                      DRIVE_READ, approval=False, idempotency="none", retry_safe=True, max_retries=2,
                      fields={"query": Field("str", max_length=300), "page_size": Field("int")}),
            Operation("files.upload", "Upload a text/markdown file to Drive (requires approval)", "external",
                      DRIVE_FILE, approval=True, idempotency="content_hash", retry_safe=False,
                      rate_limit_per_minute=20, timeout_seconds=60.0,
                      fields={"name": Field("str", required=True, max_length=200),
                              "content": Field("str", required=True, max_length=50000),
                              "mime_type": Field("str", max_length=100)}),
        ))


def youtube_spec() -> ConnectorSpec:
    return ConnectorSpec(
        id="youtube", name="YouTube", version="1.0.0", auth="oauth2",
        description="Search YouTube videos (read-only). Upload/publish is not implemented.",
        scopes=YOUTUBE_READ,
        setup=("Enable the YouTube Data API v3 for the same OAuth client, place its credentials in "
               "config/connectors/youtube.json (chmod 600), then run "
               "`cortexos connectors authorize youtube`."),
        operations=(
            Operation("videos.search", "Search YouTube videos (read-only)", "read",
                      YOUTUBE_READ, approval=False, idempotency="none", retry_safe=True, max_retries=2,
                      rate_limit_per_minute=30,
                      fields={"query": Field("str", required=True, max_length=200),
                              "max_results": Field("int")}),
        ))


def build_google_connectors(store, transport=None) -> list[GoogleConnector]:
    return [GmailConnector(gmail_spec(), store, transport),
            CalendarConnector(calendar_spec(), store, transport),
            DriveConnector(drive_spec(), store, transport),
            YouTubeConnector(youtube_spec(), store, transport)]
