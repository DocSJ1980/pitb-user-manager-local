import json
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests
from bs4 import BeautifulSoup


BASE_URL = "https://dashboard-tracking.punjab.gov.pk"
LOGIN_PATH = "/users/sign_in"
MOBILE_USERS_PATH = "/mobile_users"
NEW_MOBILE_USER_PATH = "/mobile_users/new"
TIMEOUT_SECONDS = 30
SOURCE_SHEET = "Activation"

DISTRICT_ID = "31"
DEPARTMENT_ID = "129"
TEHSIL_IDS_BY_TOWN = {
    "cantt": ("149", "148"),
    "rcb": ("149", "148"),
    "rawal town": ("137",),
    "potohar town": ("145", "146"),
    "taxila": ("138", "147", "139"),
    "taxilla": ("138", "147", "139"),
    "gujar khan": ("144",),
    "gujjar khan": ("144",),
    "kahuta": ("140",),
    "kallar syedan": ("141",),
}
TAG_CATEGORY_IDS = ("1", "2", "3", "4", "5", "6")
EDIT_PATH_RE = re.compile(r"^/mobile_users/\d+/edit$")
CAPTCHA_RE = re.compile(r"What is\s+(\d+)\s*([+\-])\s*(\d+)\s*\?", re.IGNORECASE)


def find_workbook(directory: Path = Path(".")) -> Path:
    workbooks = sorted(directory.glob("*.xlsx"))
    if not workbooks:
        raise FileNotFoundError("No .xlsx workbook found in the project directory")
    if len(workbooks) > 1:
        names = ", ".join(workbook.name for workbook in workbooks)
        raise RuntimeError(f"Expected one workbook, found: {names}")
    return workbooks[0]


def source_rows(path: Path) -> pd.DataFrame:
    try:
        rows = pd.read_excel(
            path,
            sheet_name=SOURCE_SHEET,
            dtype="string",
            engine="openpyxl",
        )
    except ValueError as error:
        if "Worksheet named" not in str(error):
            raise
        sheet_names = pd.ExcelFile(path, engine="openpyxl").sheet_names
        if len(sheet_names) != 1:
            raise ValueError(
                f"No {SOURCE_SHEET} sheet and {len(sheet_names)} sheets present; "
                f"expected exactly one fallback sheet, found: {', '.join(sheet_names)}"
            ) from error
        rows = pd.read_excel(
            path,
            sheet_name=sheet_names[0],
            dtype="string",
            engine="openpyxl",
        )
    if rows.empty:
        raise ValueError(f"The {SOURCE_SHEET} sheet has no records")
    return rows


def required_value(row: pd.Series, column: str) -> str:
    value = row[column]
    if pd.isna(value) or value == "":
        raise ValueError(f"Registration has no {column} value")
    return str(value)


def optional_value(row: pd.Series, column: str) -> str:
    value = row[column]
    return "" if pd.isna(value) else str(value)


def build_name(row: pd.Series) -> str:
    name = required_value(row, "Name")
    if "F/H Name" not in row.index:
        return " ".join(name.split())
    parent_or_husband_name = required_value(row, "F/H Name")
    gender = required_value(row, "Gender")

    if gender == "Female":
        return name + " W/O D/O " + parent_or_husband_name
    if gender == "Male":
        return name + " S/O " + parent_or_husband_name
    raise ValueError(f"Unsupported gender for first registration: {gender!r}")


def build_mobile_user_payload(row: pd.Series, csrf_token: str) -> list[tuple[str, str]]:
    cnic = required_value(row, "CNIC")
    contact_number = optional_value(row, "Cell No")
    town = required_value(row, "Town")
    tehsil_ids = TEHSIL_IDS_BY_TOWN.get(town.strip().casefold())
    if tehsil_ids is None:
        raise ValueError(f"No tehsil mapping configured for town: {town!r}")

    payload = [
        ("authenticity_token", csrf_token),
        ("mobile_user[name]", build_name(row)),
        ("mobile_user[cnic]", cnic),
        ("mobile_user[contact_no]", contact_number),
        ("mobile_user[username]", cnic),
        ("mobile_user[password]", cnic),
        ("mobile_user[password_confirmation]", cnic),
        ("mobile_user[division_id]", ""),
        ("mobile_user[district_id]", DISTRICT_ID),
        ("mobile_user[tehsil_ids][]", ""),
        *(("mobile_user[tehsil_ids][]", tehsil_id) for tehsil_id in tehsil_ids),
        ("mobile_user[uc_ids][]", ""),
        ("mobile_user[department_id]", DEPARTMENT_ID),
        ("mobile_user[tag_category_ids][]", ""),
        *(("mobile_user[tag_category_ids][]", tag_id) for tag_id in TAG_CATEGORY_IDS),
        ("mobile_user[status]", "true"),
        ("mobile_user[training_and_monnitoring]", "false"),
    ]
    return payload


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
    result = int(left) + int(right) if operator == "+" else int(left) - int(right)
    return str(result)


def validation_messages(html: str) -> set[str]:
    soup = BeautifulSoup(html, "html.parser")
    return {
        element.get_text(" ", strip=True)
        for element in soup.select(".error, .invalid-feedback, .help-block, .field_with_errors")
        if element.get_text(" ", strip=True)
    }


def response_messages(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return sorted(
        {
            element.get_text(" ", strip=True)
            for element in soup.select(".alert, .notice, .error, .invalid-feedback, .help-block")
            if element.get_text(" ", strip=True)
        }
    )


def edit_links(html: str) -> list[str]:
    links = set()
    for anchor in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = str(anchor["href"])
        path = urlsplit(href).path
        if EDIT_PATH_RE.fullmatch(path):
            links.add(urljoin(BASE_URL, href))
    return sorted(links)


def update_mobile_user_payload(row: pd.Series, csrf_token: str) -> list[tuple[str, str]]:
    return [("_method", "patch"), *build_mobile_user_payload(row, csrf_token)]


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


def search_and_fetch_edit(session: requests.Session, row: pd.Series) -> dict[str, object]:
    username = required_value(row, "CNIC")
    response = session.get(
        f"{BASE_URL}{MOBILE_USERS_PATH}",
        params={
            "mobile_user[username]": username,
            "district_id": "",
            "tehsil_id": "",
            "parent_department": "",
            "department": "",
            "status": "",
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    links = edit_links(response.text)
    result: dict[str, object] = {
        "search_url": response.url,
        "search_status": response.status_code,
        "matching_records": len(links),
    }
    if len(links) != 1:
        return result

    edit_response = session.get(links[0], timeout=TIMEOUT_SECONDS)
    edit_response.raise_for_status()
    edit_soup = BeautifulSoup(edit_response.text, "html.parser")
    heading = edit_soup.select_one("h1, h2, h3, h4, h5")
    update_url = urljoin(BASE_URL, urlsplit(links[0]).path.removesuffix("/edit"))
    patch_response = session.post(
        update_url,
        data=update_mobile_user_payload(row, parse_csrf_token(edit_response.text)),
        headers={"Referer": edit_response.url},
        timeout=TIMEOUT_SECONDS,
    )
    patch_response.raise_for_status()
    result.update(
        {
            "edit_url": edit_response.url,
            "edit_status": edit_response.status_code,
            "edit_heading": heading.get_text(" ", strip=True) if heading else None,
            "patch_url": patch_response.url,
            "patch_status": patch_response.status_code,
            "patch_redirects": [response.status_code for response in patch_response.history],
            "patch_messages": response_messages(patch_response.text),
        }
    )
    return result


def process_registration(session: requests.Session, row: pd.Series) -> dict[str, object]:
    new_form = session.get(f"{BASE_URL}{NEW_MOBILE_USER_PATH}", timeout=TIMEOUT_SECONDS)
    new_form.raise_for_status()
    create_response = session.post(
        f"{BASE_URL}{MOBILE_USERS_PATH}",
        data=build_mobile_user_payload(row, parse_csrf_token(new_form.text)),
        headers={"Referer": new_form.url},
        timeout=TIMEOUT_SECONDS,
    )
    create_response.raise_for_status()

    messages = validation_messages(create_response.text)
    result: dict[str, object] = {
        "create_url": create_response.url,
        "create_status": create_response.status_code,
        "validation_messages": sorted(messages),
    }
    duplicate_messages = {"CNIC should be unique", "User Name should be unique"}
    if duplicate_messages <= messages:
        result["duplicate_lookup"] = search_and_fetch_edit(session, row)

    return result


def main() -> None:
    username = os.environ.get("PITB_USERNAME")
    password = os.environ.get("PITB_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set PITB_USERNAME and PITB_PASSWORD before running this script")

    rows = source_rows(find_workbook())
    session = requests.Session()
    session.headers.update({"User-Agent": "pitb-user-manager/0.1"})
    login(session, username, password)

    summary = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "skipped_duplicates": 0,
        "unresolved_duplicates": 0,
        "failed": 0,
    }
    processed_cnics: set[str] = set()
    for index, row in rows.iterrows():
        result: dict[str, object] = {"excel_row": int(index) + 2}
        try:
            result["source_sr"] = required_value(row, "Sr")
            cnic = required_value(row, "CNIC")
            result["cnic"] = cnic
            if cnic in processed_cnics:
                summary["skipped_duplicates"] += 1
                result["operation"] = "skipped_duplicate_source_row"
                summary["processed"] += 1
                print(json.dumps(result))
                continue

            result.update(process_registration(session, row))
            processed_cnics.add(cnic)
            duplicate_lookup = result.get("duplicate_lookup")
            validation_messages = result["validation_messages"]
            messages = set(validation_messages) if isinstance(validation_messages, list) else set()
            if isinstance(duplicate_lookup, dict) and "patch_status" in duplicate_lookup:
                summary["updated"] += 1
                result["operation"] = "updated"
            elif {"CNIC should be unique", "User Name should be unique"} <= messages:
                summary["unresolved_duplicates"] += 1
                result["operation"] = "duplicate_not_updated"
            elif messages:
                summary["failed"] += 1
                result["operation"] = "validation_failed"
            else:
                summary["created"] += 1
                result["operation"] = "created"
        except (requests.RequestException, RuntimeError, ValueError) as error:
            summary["failed"] += 1
            result.update({"operation": "failed", "error": str(error)})

        summary["processed"] += 1
        print(json.dumps(result))

    print(json.dumps({"summary": summary}))


if __name__ == "__main__":
    main()
