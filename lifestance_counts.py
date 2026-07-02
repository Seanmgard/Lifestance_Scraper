import argparse
import asyncio
import datetime as dt
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)


BASE_URL = "https://lifestance.com"
TODAY = dt.date.today().isoformat()

DEFAULT_PROVIDER_TYPES = ["therapist", "psychiatrist"]

SENDER_EMAIL = "seanmgard@gmail.com"

RECIPIENT_EMAILS = [
    "sean.gardner@ubs.com",
    "seanmgard@gmail.com",
]

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


STATE_ABBR_TO_NAME = {
    "al": "Alabama",
    "ak": "Alaska",
    "az": "Arizona",
    "ar": "Arkansas",
    "ca": "California",
    "co": "Colorado",
    "ct": "Connecticut",
    "de": "Delaware",
    "dc": "District of Columbia",
    "fl": "Florida",
    "ga": "Georgia",
    "hi": "Hawaii",
    "id": "Idaho",
    "il": "Illinois",
    "in": "Indiana",
    "ia": "Iowa",
    "ks": "Kansas",
    "ky": "Kentucky",
    "la": "Louisiana",
    "me": "Maine",
    "md": "Maryland",
    "ma": "Massachusetts",
    "mi": "Michigan",
    "mn": "Minnesota",
    "ms": "Mississippi",
    "mo": "Missouri",
    "mt": "Montana",
    "ne": "Nebraska",
    "nv": "Nevada",
    "nh": "New Hampshire",
    "nj": "New Jersey",
    "nm": "New Mexico",
    "ny": "New York",
    "nc": "North Carolina",
    "nd": "North Dakota",
    "oh": "Ohio",
    "ok": "Oklahoma",
    "or": "Oregon",
    "pa": "Pennsylvania",
    "ri": "Rhode Island",
    "sc": "South Carolina",
    "sd": "South Dakota",
    "tn": "Tennessee",
    "tx": "Texas",
    "ut": "Utah",
    "vt": "Vermont",
    "va": "Virginia",
    "wa": "Washington",
    "wv": "West Virginia",
    "wi": "Wisconsin",
    "wy": "Wyoming",
}


GET_TOTAL_JS = """
() => {
  const toInt = value => {
    if (value === null || value === undefined) return null;

    const cleaned = String(value)
      .replace(/,/g, "")
      .trim();

    const n = parseInt(cleaned, 10);

    return Number.isFinite(n) ? n : null;
  };

  const dataTotals = Array.from(
    document.querySelectorAll("[data-total]")
  )
    .map(el => toInt(el.getAttribute("data-total")))
    .filter(n => n !== null);

  const bodyText = document.body
    ? document.body.innerText
    : "";

  const approxMatch = bodyText.match(
    /Choose from approximately\\s+([\\d,]+)/i
  );

  const approxTotal = approxMatch
    ? toInt(approxMatch[1])
    : null;

  return {
    data_total: dataTotals.length
      ? Math.max(...dataTotals)
      : null,

    all_data_totals: dataTotals,

    approximate_text_total: approxTotal
  };
}
"""


def parse_csv_arg(value, default_values):
    if not value:
        return default_values

    return [
        x.strip().lower()
        for x in value.split(",")
        if x.strip()
    ]


async def close_popups_if_present(page):
    possible_buttons = [
        "Accept",
        "Accept All",
        "I Accept",
        "Agree",
        "Close",
        "No Thanks",
    ]

    for text in possible_buttons:
        try:
            locator = page.get_by_text(
                text,
                exact=False,
            )

            count = await locator.count()

            for i in range(min(count, 3)):
                item = locator.nth(i)

                if await item.is_visible():
                    await item.click(timeout=2000)
                    await page.wait_for_timeout(500)
                    return

        except Exception:
            continue


async def get_dom_total(page):
    try:
        return await page.evaluate(GET_TOTAL_JS)

    except Exception:
        return {
            "data_total": None,
            "all_data_totals": [],
            "approximate_text_total": None,
        }


async def scrape_state_type_count(
    context,
    state,
    provider_type,
    wait_ms=3000,
):
    state = state.lower()
    provider_type = provider_type.lower()

    state_name = STATE_ABBR_TO_NAME.get(
        state,
        state.upper(),
    )

    url = (
        f"{BASE_URL}/provider/"
        f"{provider_type}/{state}/"
    )

    page = await context.new_page()

    result = {
        "state": state_name,
        "state_abbr": state.upper(),
        "provider_type": provider_type,
        "count": 0,
        "source_url": url,
        "status": "",
    }

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        if (
            response is not None
            and response.status >= 400
        ):
            result["status"] = (
                f"http_{response.status}"
            )
            return result

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=15000,
            )

        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(wait_ms)

        await close_popups_if_present(page)

        total_info = await get_dom_total(page)

        data_total = total_info.get(
            "data_total"
        )

        approximate_total = total_info.get(
            "approximate_text_total"
        )

        if data_total is not None:
            result["count"] = int(data_total)
            result["status"] = "ok_data_total"

        elif approximate_total is not None:
            result["count"] = int(
                approximate_total
            )

            result["status"] = (
                "ok_approximate_text_total"
            )

        else:
            result["count"] = 0
            result["status"] = "no_total_found"

        return result

    except Exception as exc:
        result["count"] = 0

        result["status"] = (
            f"scrape_error: "
            f"{type(exc).__name__}: {exc}"
        )

        return result

    finally:
        await page.close()


async def scrape_all_counts(
    states,
    provider_types,
    headed=False,
    wait_ms=3000,
):
    rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=not headed
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1200,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
        )

        for state in states:
            for provider_type in provider_types:
                print(
                    "Scraping count: "
                    f"{provider_type} / "
                    f"{state.upper()}"
                )

                result = (
                    await scrape_state_type_count(
                        context=context,
                        state=state,
                        provider_type=provider_type,
                        wait_ms=wait_ms,
                    )
                )

                rows.append(result)

                print(
                    f"  count={result['count']} | "
                    f"status={result['status']}"
                )

        await context.close()
        await browser.close()

    return pd.DataFrame(rows)


def make_state_counts(raw_counts_df):
    if raw_counts_df.empty:
        return pd.DataFrame(
            columns=[
                "state",
                "state_abbr",
                "therapist",
                "psychiatrist",
                "total",
            ]
        )

    pivot = (
        raw_counts_df.pivot_table(
            index=[
                "state",
                "state_abbr",
            ],
            columns="provider_type",
            values="count",
            aggfunc="sum",
            fill_value=0,
        )
        .reset_index()
    )

    for provider_type in DEFAULT_PROVIDER_TYPES:
        if provider_type not in pivot.columns:
            pivot[provider_type] = 0

    pivot["therapist"] = (
        pivot["therapist"].astype(int)
    )

    pivot["psychiatrist"] = (
        pivot["psychiatrist"].astype(int)
    )

    pivot["total"] = (
        pivot["therapist"]
        + pivot["psychiatrist"]
    )

    state_counts = pivot[
        [
            "state",
            "state_abbr",
            "therapist",
            "psychiatrist",
            "total",
        ]
    ].copy()

    state_counts = (
        state_counts
        .sort_values("state")
        .reset_index(drop=True)
    )

    return state_counts


def autosize_excel_columns(
    writer,
    sheet_name,
    df,
):
    worksheet = writer.sheets[sheet_name]

    worksheet.freeze_panes(1, 0)

    for i, col in enumerate(df.columns):
        if df.empty:
            sample_values = []

        else:
            sample_values = [
                ""
                if pd.isna(x)
                else str(x)
                for x in df[col]
                .head(1000)
                .tolist()
            ]

        width = max(
            [len(str(col))]
            + [len(v) for v in sample_values]
        )

        worksheet.set_column(
            i,
            i,
            min(
                max(width + 2, 10),
                70,
            ),
        )


def write_state_counts_workbook(
    state_counts_df,
    outdir,
):
    output_path = (
        outdir
        / f"lifestance_state_counts_{TODAY}.xlsx"
    )

    with pd.ExcelWriter(
        output_path,
        engine="xlsxwriter",
    ) as writer:
        state_counts_df.to_excel(
            writer,
            sheet_name="State_Counts",
            index=False,
        )

        workbook = writer.book
        worksheet = writer.sheets[
            "State_Counts"
        ]

        header_format = workbook.add_format(
            {
                "bold": True,
                "bg_color": "#D9EAF7",
                "border": 1,
            }
        )

        for col_num, column_name in enumerate(
            state_counts_df.columns
        ):
            worksheet.write(
                0,
                col_num,
                column_name,
                header_format,
            )

        autosize_excel_columns(
            writer,
            "State_Counts",
            state_counts_df,
        )

    return output_path


def send_email_with_attachment(
    file_path: Path,
):
    """
    Send the completed Excel workbook using Gmail SMTP.

    The Gmail app password must be supplied through the
    SMTP_APP_PASSWORD environment variable.
    """

    app_password = os.getenv(
        "SMTP_APP_PASSWORD",
        "",
    ).replace(" ", "")

    if not app_password:
        print(
            "\nEmail skipped because the "
            "SMTP_APP_PASSWORD environment "
            "variable was not provided."
        )
        return False

    if not file_path.exists():
        raise FileNotFoundError(
            "The email attachment could not be "
            f"found: {file_path}"
        )

    message = EmailMessage()

    message["Subject"] = (
        f"LifeStance State Provider Counts - "
        f"{TODAY}"
    )

    message["From"] = SENDER_EMAIL

    message["To"] = ", ".join(
        RECIPIENT_EMAILS
    )

    message.set_content(
        f"""Attached is the LifeStance state-level provider count report for {TODAY}.

The workbook includes therapist and psychiatrist counts by state.

Sender: {SENDER_EMAIL}

This email was generated automatically by the LifeStance GitHub Actions workflow.
"""
    )

    mime_type, encoding = mimetypes.guess_type(
        str(file_path)
    )

    if mime_type is None or encoding is not None:
        mime_type = (
            "application/"
            "vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )

    main_type, sub_type = mime_type.split(
        "/",
        1,
    )

    with file_path.open("rb") as attachment:
        message.add_attachment(
            attachment.read(),
            maintype=main_type,
            subtype=sub_type,
            filename=file_path.name,
        )

    try:
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=60,
        ) as smtp:
            smtp.login(
                SENDER_EMAIL,
                app_password,
            )

            smtp.send_message(message)

    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            "Gmail authentication failed. "
            "Confirm that SMTP_APP_PASSWORD is "
            "a valid Google app password for "
            f"{SENDER_EMAIL}."
        ) from exc

    except smtplib.SMTPException as exc:
        raise RuntimeError(
            f"Email sending failed: {exc}"
        ) from exc

    print(
        "\nEmail sent successfully from "
        f"{SENDER_EMAIL} to:"
    )

    for recipient in RECIPIENT_EMAILS:
        print(f"  {recipient}")

    return True


async def main_async():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--outdir",
        default="lifestance_output",
        help=(
            "Folder where Excel output "
            "will be saved."
        ),
    )

    parser.add_argument(
        "--states",
        default=None,
        help=(
            "Comma-separated state abbreviations. "
            "Example: az,ny,ca. "
            "Defaults to all states."
        ),
    )

    parser.add_argument(
        "--types",
        default=None,
        help=(
            "Comma-separated provider types. "
            "Example: therapist,psychiatrist. "
            "Defaults to both."
        ),
    )

    parser.add_argument(
        "--headed",
        action="store_true",
        help=(
            "Run browser visibly instead of "
            "headless. Useful for debugging."
        ),
    )

    parser.add_argument(
        "--wait-ms",
        type=int,
        default=3000,
        help=(
            "Initial wait after page load "
            "in milliseconds."
        ),
    )

    parser.add_argument(
        "--skip-email",
        action="store_true",
        help=(
            "Create the workbook without "
            "sending an email."
        ),
    )

    args, unknown = parser.parse_known_args()

    states = parse_csv_arg(
        args.states,
        list(STATE_ABBR_TO_NAME.keys()),
    )

    provider_types = parse_csv_arg(
        args.types,
        DEFAULT_PROVIDER_TYPES,
    )

    outdir = Path(args.outdir)

    outdir.mkdir(
        parents=True,
        exist_ok=True,
    )

    raw_counts_df = await scrape_all_counts(
        states=states,
        provider_types=provider_types,
        headed=args.headed,
        wait_ms=args.wait_ms,
    )

    state_counts_df = make_state_counts(
        raw_counts_df
    )

    output_path = write_state_counts_workbook(
        state_counts_df=state_counts_df,
        outdir=outdir,
    )

    if args.skip_email:
        print(
            "\nEmail skipped because "
            "--skip-email was used."
        )
    else:
        send_email_with_attachment(
            output_path
        )

    print("\nDone.")
    print(f"Workbook: {output_path}")

    print("\nState counts:")
    print(
        state_counts_df.to_string(
            index=False
        )
    )


def run_main():
    try:
        running_loop = (
            asyncio.get_running_loop()
        )

    except RuntimeError:
        running_loop = None

    if (
        running_loop
        and running_loop.is_running()
    ):
        try:
            import nest_asyncio

        except ImportError:
            raise ImportError(
                "You are running inside VS Code "
                "Interactive/Jupyter. Install "
                "nest_asyncio first with: "
                "pip install nest_asyncio"
            )

        nest_asyncio.apply()

        running_loop.run_until_complete(
            main_async()
        )

    else:
        asyncio.run(main_async())


if __name__ == "__main__":
    run_main()