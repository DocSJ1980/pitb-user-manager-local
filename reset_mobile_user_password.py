"""Reset exactly one PITB mobile user's password without changing other fields.

Administrative dashboard credentials are read from PITB_USERNAME and PITB_PASSWORD.
Supply the mobile account through MOBILE_USER_USERNAME and MOBILE_USER_PASSWORD, or
pass --username and enter the new password at the secure prompt.
"""

import argparse
import getpass
import json
import os
import re
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


BASE_URL = "https://dashboard-tracking.punjab.gov.pk"
LOGIN_PATH = "/users/sign_in"
MOBILE_USERS_PATH = "/mobile_users"
TIMEOUT_SECONDS = 30
EDIT_PATH_RE = re.compile(r"^/mobile_users/\d+/edit$")
UPDATE_PATH_RE = re.compile(r"^/mobile_users/\d+$")
CAPTCHA_RE = re.compile(r"What is\s+(\d+)\s*([+\-])\s*(\d+)\s*\?", re.IGNORECASE)
USERNAME_FIELD = "mobile_user[username]"
PASSWORD_FIELD = "mobile_user[password]"
PASSWORD_CONFIRMATION_FIELD = "mobile_user[password_confirmation]"


def parse_csrf_token(html: str) -> str:
    token = BeautifulSoup(html, "html.parser").select_one(
        'input[name="authenticity_token"]'
    )
    if token is None or not token.get("value"):
        raise ValueError("No authenticity token found in response")
    return str(token["value"])


def solve_captcha(html: str) -> str:
    match = CAPTCHA_RE.search(BeautifulSoup(html, "html.parser").get_text(" "))
    if match is None:
        raise ValueError("Could not find the login CAPTCHA question")
    left, operator, right = match.groups()
    return str(int(left) + int(right) if operator == "+" else int(left) - int(right))


def login(session: requests.Session, username: str, password: str) -> None:
    login_page = session.get(f"{BASE_URL}{LOGIN_PATH}", timeout=TIMEOUT_SECONDS)
    login_page.raise_for_status()
    response = session.post(
        f"{BASE_URL}{LOGIN_PATH}",
        data={
            "authenticity_token": parse_csrf_token(login_page.text),
            "user[username]": username,
            "user[password]": password,
            "captcha": solve_captcha(login_page.text),
        },
        headers={"Referer": login_page.url},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    if "Welcome," not in response.text:
        raise RuntimeError("Login failed; the authenticated page was not returned")


def edit_links(html: str) -> list[str]:
    links = set()
    for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = str(anchor["href"])
        if EDIT_PATH_RE.fullmatch(urlsplit(href).path):
            links.add(urljoin(BASE_URL, href))
    return sorted(links)


def selected_options(select: Tag) -> list[Tag]:
    options = select.find_all("option")
    selected = [option for option in options if option.has_attr("selected")]
    if not selected and not select.has_attr("multiple") and options:
        return [options[0]]
    return selected


def form_payload(html: str, new_password: str) -> tuple[str, list[tuple[str, str]], str]:
    """Return the update endpoint and a complete existing-form payload.

    The dashboard's edit form is the source of truth. This preserves every enabled
    field value currently submitted by a browser and adds only the requested
    password field; the current form does not render password inputs.
    """
    soup = BeautifulSoup(html, "html.parser")
    forms = [
        form
        for form in soup.find_all("form")
        if UPDATE_PATH_RE.fullmatch(urlsplit(str(form.get("action", ""))).path)
    ]
    if len(forms) != 1:
        raise ValueError(f"Expected one edit mobile-user form, found {len(forms)}")
    form = forms[0]
    update_path = urlsplit(str(form["action"])).path

    payload: list[tuple[str, str]] = []
    for control in form.find_all(["input", "select", "textarea"]):
        name = control.get("name")
        control_type = str(control.get("type", "")).lower()
        if not name or control.has_attr("disabled"):
            continue
        if control_type in {"button", "submit", "reset", "image", "file"}:
            continue
        if control_type in {"checkbox", "radio"} and not control.has_attr("checked"):
            continue
        if control.name == "select":
            payload.extend(
                (str(name), str(option.get("value", "")))
                for option in selected_options(control)
            )
        else:
            value = (
                control.get_text() if control.name == "textarea" else str(control.get("value", ""))
            )
            payload.append((str(name), value))

    names = {name for name, _ in payload}
    required_fields = {"authenticity_token", USERNAME_FIELD}
    missing_fields = required_fields - names
    if missing_fields:
        raise ValueError(
            "The edit form is missing required fields: "
            + ", ".join(sorted(missing_fields))
        )

    existing_username = next(value for name, value in payload if name == USERNAME_FIELD)
    payload = [
        (name, value)
        for name, value in payload
        if name not in {"_method", PASSWORD_FIELD, PASSWORD_CONFIRMATION_FIELD}
    ]
    payload.insert(0, ("_method", "patch"))
    # The dashboard's edit form omits password controls. Submit only the single
    # password attribute requested by the user; do not guess a confirmation field.
    payload.append((PASSWORD_FIELD, new_password))
    return urljoin(BASE_URL, update_path), payload, existing_username


def response_messages(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return sorted(
        {
            element.get_text(" ", strip=True)
            for element in soup.select(".alert, .notice, .error, .invalid-feedback, .help-block")
            if element.get_text(" ", strip=True)
        }
    )


def listing_search_parameters(
    html: str, listing_url: str, target_username: str
) -> tuple[str, list[tuple[str, str]], str]:
    """Build the listing query from the dashboard's live filter form.

    Rather than guessing the query key, serialize the current GET filter form
    and replace its one username field.
    """
    soup = BeautifulSoup(html, "html.parser")
    search_forms = [
        form
        for form in soup.find_all("form")
        if str(form.get("method", "get")).casefold() == "get"
        and urlsplit(urljoin(listing_url, str(form.get("action", "")))).path
        == MOBILE_USERS_PATH
    ]
    if len(search_forms) != 1:
        raise ValueError(
            f"Expected one mobile-user listing search form, found {len(search_forms)}"
        )

    form = search_forms[0]
    username_controls = [
        control
        for control in form.find_all("input")
        if str(control.get("type", "text")).casefold() in {"", "search", "text"}
        and control.get("name")
        and "username" in str(control["name"]).casefold()
        and not control.has_attr("disabled")
    ]
    if len(username_controls) != 1:
        candidates = sorted(
            {
                str(control.get("name"))
                for control in form.find_all(["input", "select", "textarea"])
                if control.get("name") and "username" in str(control["name"]).casefold()
            }
        )
        raise ValueError(
            "Expected one enabled text username filter in the mobile-user listing form; "
            f"found {len(username_controls)} (candidate names: {candidates})"
        )
    username_name = str(username_controls[0]["name"])

    params: list[tuple[str, str]] = []
    for control in form.find_all(["input", "select", "textarea"]):
        name = control.get("name")
        control_type = str(control.get("type", "")).casefold()
        if not name or control.has_attr("disabled"):
            continue
        if control_type in {"button", "submit", "reset", "image", "file"}:
            continue
        if control_type in {"checkbox", "radio"} and not control.has_attr("checked"):
            continue
        if control.name == "select":
            params.extend(
                (str(name), str(option.get("value", "")))
                for option in selected_options(control)
            )
        elif str(name) != username_name:
            value = (
                control.get_text()
                if control.name == "textarea"
                else str(control.get("value", ""))
            )
            params.append((str(name), value))

    search_url = urljoin(listing_url, str(form.get("action", "")))
    return search_url, [*params, (username_name, target_username)], username_name


def reset_password(
    session: requests.Session, target_username: str, new_password: str, dry_run: bool
) -> dict[str, object]:
    listing_response = session.get(
        f"{BASE_URL}{MOBILE_USERS_PATH}", timeout=TIMEOUT_SECONDS
    )
    listing_response.raise_for_status()
    search_url, params, username_filter_name = listing_search_parameters(
        listing_response.text, listing_response.url, target_username
    )
    search_response = session.get(search_url, params=params, timeout=TIMEOUT_SECONDS)
    search_response.raise_for_status()
    links = edit_links(search_response.text)
    result: dict[str, object] = {
        "matching_records": len(links),
        "username_filter_field": username_filter_name,
    }
    if len(links) != 1:
        raise RuntimeError(
            f"Refusing to update: username search returned {len(links)} edit records, expected exactly 1"
        )

    edit_response = session.get(links[0], timeout=TIMEOUT_SECONDS)
    edit_response.raise_for_status()
    update_url, payload, existing_username = form_payload(edit_response.text, new_password)
    if existing_username != target_username:
        raise RuntimeError(
            "Refusing to update: the edit form username does not exactly match the search username"
        )

    result["form_username_matches_search"] = True
    if dry_run:
        result["operation"] = "dry_run_verified"
        return result

    patch_response = session.post(
        update_url,
        data=payload,
        headers={"Referer": edit_response.url},
        timeout=TIMEOUT_SECONDS,
    )
    patch_response.raise_for_status()
    messages = response_messages(patch_response.text)
    errors = [
        message
        for message in messages
        if "error" in message.casefold() or "invalid" in message.casefold()
    ]
    if errors:
        raise RuntimeError("Dashboard rejected password update: " + "; ".join(errors))
    result.update(
        {
            "operation": "password_updated",
            "patch_status": patch_response.status_code,
            "patch_redirects": [response.status_code for response in patch_response.history],
            "messages": messages,
        }
    )
    return result


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset one existing PITB mobile user's password while preserving all other values."
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("MOBILE_USER_USERNAME"),
        help="Mobile-user username (or set MOBILE_USER_USERNAME).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Authenticate and verify the exact match/form only; do not submit an update.",
    )
    return parser.parse_args()


def main() -> None:
    args = arguments()
    admin_username = os.environ.get("PITB_USERNAME")
    admin_password = os.environ.get("PITB_PASSWORD")
    target_username = args.username
    new_password = os.environ.get("MOBILE_USER_PASSWORD")
    if not admin_username or not admin_password:
        raise RuntimeError("Set PITB_USERNAME and PITB_PASSWORD before running this script")
    if not target_username:
        raise RuntimeError("Pass --username or set MOBILE_USER_USERNAME")
    if not new_password:
        new_password = getpass.getpass("New mobile-user password: ")
    if not new_password:
        raise RuntimeError("A non-empty new mobile-user password is required")

    session = requests.Session()
    session.headers.update({"User-Agent": "pitb-user-manager/0.1"})
    login(session, admin_username, admin_password)
    print(json.dumps(reset_password(session, target_username, new_password, args.dry_run)))


if __name__ == "__main__":
    main()
