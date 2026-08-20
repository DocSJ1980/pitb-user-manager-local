import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup
from bs4.element import Tag


BASE_URL = "https://dashboard-tracking.punjab.gov.pk"
LOGIN_PATH = "/users/sign_in"
MOBILE_USERS_PATH = "/mobile_users"
WORKBOOK_PATH = Path("Sweep Staff access.xlsx")
SOURCE_SHEET = "Sheet2"
TIMEOUT_SECONDS = 30
TEHSIL_FIELD = "mobile_user[tehsil_ids][]"
TEHSIL_IDS_BY_ACCESS = {
    "cantt": ("149", "148"),
    "potohar town": ("145", "146"),
}
EDIT_PATH_RE = re.compile(r"^/mobile_users/\d+/edit$")
UPDATE_PATH_RE = re.compile(r"^/mobile_users/\d+$")
CAPTCHA_RE = re.compile(r"What is\s+(\d+)\s*([+\-])\s*(\d+)\s*\?", re.IGNORECASE)


def required_value(row: pd.Series, column: str) -> str:
    value = row[column]
    if pd.isna(value) or value == "":
        raise ValueError(f"Access row has no {column} value")
    return str(value)


def access_rows() -> pd.DataFrame:
    rows = pd.read_excel(
        WORKBOOK_PATH,
        sheet_name=SOURCE_SHEET,
        dtype="string",
        engine="openpyxl",
    )
    required_columns = {"CNIC", "Access Required for"}
    missing_columns = required_columns - set(rows.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(sorted(missing_columns))}")
    if rows.empty:
        raise ValueError(f"The {SOURCE_SHEET} sheet has no rows")
    return rows


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


def update_payload(
    html: str, additional_tehsil_ids: tuple[str, ...]
) -> tuple[str, list[tuple[str, str]], list[str], list[str]]:
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
    if not UPDATE_PATH_RE.fullmatch(update_path):
        raise ValueError(f"Unexpected update path: {update_path}")

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
            for option in selected_options(control):
                payload.append((str(name), str(option.get("value", ""))))
            continue

        value = control.get_text() if control.name == "textarea" else str(control.get("value", ""))
        # Empty passwords must be omitted so the account keeps its current password.
        if control_type == "password" and not value:
            continue
        payload.append((str(name), value))

    if not any(name == "authenticity_token" for name, _ in payload):
        raise ValueError("The edit form has no authenticity token")

    existing_tehsil_ids = [
        value for name, value in payload if name == TEHSIL_FIELD and value
    ]
    final_tehsil_ids = list(dict.fromkeys([*existing_tehsil_ids, *additional_tehsil_ids]))
    payload = [
        (name, value)
        for name, value in payload
        if name not in {"_method", TEHSIL_FIELD}
    ]
    payload.insert(0, ("_method", "patch"))
    payload.extend([(TEHSIL_FIELD, ""), *((TEHSIL_FIELD, value) for value in final_tehsil_ids)])

    return (
        urljoin(BASE_URL, update_path),
        payload,
        existing_tehsil_ids,
        final_tehsil_ids,
    )


def process_access_row(session: requests.Session, row: pd.Series) -> dict[str, object]:
    cnic = required_value(row, "CNIC")
    access_required = required_value(row, "Access Required for")
    additional_tehsil_ids = TEHSIL_IDS_BY_ACCESS.get(access_required.strip().casefold())
    if additional_tehsil_ids is None:
        raise ValueError(f"No tehsil mapping configured for access: {access_required!r}")

    search_response = session.get(
        f"{BASE_URL}{MOBILE_USERS_PATH}",
        params={
            "mobile_user[username]": cnic,
            "district_id": "",
            "tehsil_id": "",
            "parent_department": "",
            "department": "",
            "status": "",
        },
        timeout=TIMEOUT_SECONDS,
    )
    search_response.raise_for_status()
    links = edit_links(search_response.text)
    result: dict[str, object] = {
        "search_url": search_response.url,
        "search_status": search_response.status_code,
        "matching_records": len(links),
        "requested_tehsil_ids": list(additional_tehsil_ids),
    }
    if len(links) != 1:
        return result

    edit_response = session.get(links[0], timeout=TIMEOUT_SECONDS)
    edit_response.raise_for_status()
    update_url, payload, existing_tehsil_ids, final_tehsil_ids = update_payload(
        edit_response.text, additional_tehsil_ids
    )
    patch_response = session.post(
        update_url,
        data=payload,
        headers={"Referer": edit_response.url},
        timeout=TIMEOUT_SECONDS,
    )
    patch_response.raise_for_status()
    result.update(
        {
            "edit_url": edit_response.url,
            "edit_status": edit_response.status_code,
            "existing_tehsil_ids": existing_tehsil_ids,
            "final_tehsil_ids": final_tehsil_ids,
            "patch_url": patch_response.url,
            "patch_status": patch_response.status_code,
            "patch_redirects": [response.status_code for response in patch_response.history],
        }
    )
    return result


def main() -> None:
    username = os.environ.get("PITB_USERNAME")
    password = os.environ.get("PITB_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set PITB_USERNAME and PITB_PASSWORD before running this script")

    rows = access_rows()
    session = requests.Session()
    session.headers.update({"User-Agent": "pitb-user-manager/0.1"})
    login(session, username, password)

    summary = {"processed": 0, "updated": 0, "unresolved": 0, "failed": 0}
    for index, row in rows.iterrows():
        result: dict[str, object] = {"excel_row": int(index) + 2}
        try:
            result["cnic"] = required_value(row, "CNIC")
            result.update(process_access_row(session, row))
            if "patch_status" in result:
                summary["updated"] += 1
                result["operation"] = "updated"
            else:
                summary["unresolved"] += 1
                result["operation"] = "not_updated"
        except (requests.RequestException, RuntimeError, ValueError) as error:
            summary["failed"] += 1
            result.update({"operation": "failed", "error": str(error)})

        summary["processed"] += 1
        print(json.dumps(result))

    print(json.dumps({"summary": summary}))


if __name__ == "__main__":
    main()
