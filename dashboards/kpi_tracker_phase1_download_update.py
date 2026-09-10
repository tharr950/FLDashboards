#!/usr/bin/env python3
"""
kpi_tracker_phase1_download_update.py

PHASE 1 of the KPI Tracker pipeline (converted from KPITrackers.ipynb's
cell 12 -- the notebook had a stale earlier duplicate of this in cell 6;
this file is that later, working version, cleaned up and moved into the
regular pipeline instead of a notebook).

For each tutor: downloads their current KPI Tracker from SharePoint,
appends their row from Metrics_File.xlsx (built by
build_metrics_file_for_trackers.py -- run that first), and saves the
finished file locally to
    Desktop/KPI_Trackers/<Faculty Leader>/<Tutor> KPI Tracker.xlsx

Nothing gets uploaded back to SharePoint by this script. Between this
and phase 2, YOU manually upload the contents of each
Desktop/KPI_Trackers/<Faculty Leader>/ folder into that Faculty Leader's
"aa_Metrics_Upload" staging folder in SharePoint (General > IL Team
Folders > <Faculty Leader>'s Team > aa_Metrics_Upload) -- a plain
drag-and-drop browser upload. Then kpi_tracker_phase2_copy_from_staging.py
copies each file from staging into the tutor's real KPI Tracker folder.
"""

import os
import shutil
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl
import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

LOGIN_EMAIL = 'tyler.harrington@revolutionprep.com'
LOGIN_PASSWORD = 'Cattongue!950'

# =============================================================================
# CONFIG
# =============================================================================

# Kristin Haase-Alvey -- never had step 1 succeed at all (died at login
# every time before the persistent-profile/login fixes went in), so this
# is a plain fresh run, no retry/dedup concerns.
FACULTY_LEADERS_TO_RUN = ['Kristin Haase-Alvey']

# Faculty Leaders to always skip, regardless of the setting above.
EXCLUDED_FACULTY_LEADERS = ['Katherine Marino', 'Nikki Pencak']

# Set this to re-run only specific tutors instead of every tutor for a
# leader -- important after a partial run, since this script always
# appends at the next empty row: re-running someone who already succeeded
# would give them a DUPLICATE row for this period. Leave as None to run
# everyone normally. When set, ONLY the leaders listed here run, and ONLY
# the tutors listed for each one (FACULTY_LEADERS_TO_RUN and
# EXCLUDED_FACULTY_LEADERS are ignored while this is set).
# Eleanor Mancilla only -- Tim Page already succeeded in the previous
# run, so leaving him out here to avoid a duplicate row. Master_Tutor.csv
# had her under Katherine Marino (stale); corrected to Annelies de Groot
# before this run.
RETRY_ONLY = {'Annelies de Groot': ['Eleanor Mancilla']}
# Example: RETRY_ONLY = {'Ela Cross': ['Some Tutor']}

# Total attempts per tutor (1 initial try + retries) before giving up.
MAX_TUTOR_ATTEMPTS = 3

# If this many tutors IN A ROW fail completely within one leader's run, cool
# down before continuing instead of blasting through the rest at a broken rate.
CONSECUTIVE_FAILURE_COOLDOWN_THRESHOLD = 3
COOLDOWN_SECONDS = 120

# Confirmed non-functional on this machine (the header XPath never matched --
# every attempt just wasted ~10s and fell through). Off by default; only
# flip on if you get a selector for the Name header confirmed to work here.
ENABLE_SORT_FALLBACK = False

# Run every Faculty Leader in its own browser session, IN PARALLEL, instead
# of one after another. Was 2, but the last 2-parallel run (Geoff +
# Kristin sharing a slot with Ela) killed BOTH of them at the exact same
# login step (couldn't find the email field, id="i0116") while the leader
# that ran mostly alone succeeded -- consistent with 2 simultaneous logins
# overloading page-load timing. login_and_reach_il_team_folders now waits
# explicitly instead of a flat sleep(3), which should fix the real cause,
# but running these last 2 sequentially removes the risk entirely while we
# confirm the fix. Bump back to 2 once this run is clean.
MAX_PARALLEL_LEADERS = 1

# Root cause of the login stalls found and fixed (idSIButton9 reused
# across 3 different pages -- see click_primary_login_button) -- back to
# headless for normal runs.
HEADLESS = True

# Confirmed twice: clicking SharePoint's "Create or upload" command crashes
# the browser (a split-button whose main click IS "Upload files" rather than
# a menu toggle, firing a native OS file dialog Selenium can't see/dismiss).
# Off by default -- every finished tracker still gets saved to
# Desktop/KPI_Trackers/<Faculty Leader>/ and just needs a manual
# drag-and-drop upload, which is fast and doesn't touch this button at all.
ENABLE_REUPLOAD = False

desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")

# Shared across all 3 KPI tracker scripts on purpose (same directory name)
# -- a fresh, cookie-less Chrome profile every run means Microsoft never
# recognizes it as a "known device," and after enough automated sign-ins
# in one day its risk engine starts demanding step-up verification on
# every single run regardless of timing (confirmed via screenshot: "Let's
# keep your account secure"). Pointing every script at the SAME persistent
# profile means clearing that verification once, in any one of them,
# clears it for all three going forward.
PERSISTENT_PROFILE_DIR = os.path.join(desktop_path, ".automation_chrome_profile")
os.makedirs(PERSISTENT_PROFILE_DIR, exist_ok=True)

# This is where Chrome actually saves downloads, and where process_tutor()
# looks for the file afterward -- these two MUST always match.
downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
print(f"Downloading to: {downloads_path}")

error_screenshot_dir = os.path.join(desktop_path, "automation_errors")
os.makedirs(error_screenshot_dir, exist_ok=True)

# Finished KPI Trackers get filed into Desktop/KPI_Trackers/<Faculty Leader>/
# instead of all landing flat in one folder.
output_base_dir = os.path.join(desktop_path, "KPI_Trackers")
os.makedirs(output_base_dir, exist_ok=True)

MASTER_TUTOR_PATH = os.path.join(desktop_path, "Master_Tutor.csv")
MasterTutor = pd.read_csv(MASTER_TUTOR_PATH)

# Metrics_File.xlsx is built by build_metrics_file_for_trackers.py and
# written to the Desktop. Load it here instead of depending on a separate
# notebook cell having already run.
metrics_sheet = None
for filename in os.listdir(desktop_path):
    if "Metrics_File" in filename and filename.endswith('.xlsx'):
        metrics_path = os.path.join(desktop_path, filename)
        metrics_workbook = openpyxl.load_workbook(metrics_path)
        metrics_sheet = metrics_workbook.active
        print(f"Loaded metrics sheet from: {metrics_path}")
        break
if metrics_sheet is None:
    raise FileNotFoundError(
        f"No 'Metrics_File*.xlsx' found in {desktop_path} -- run "
        f"build_metrics_file_for_trackers.py first."
    )

# `metrics_sheet` is read by multiple leader-threads concurrently; access is
# serialized with this lock -- reads are fast, so this doesn't meaningfully
# cut into the parallel speedup.
METRICS_LOCK = threading.Lock()

if RETRY_ONLY is not None:
    faculty_leaders = list(RETRY_ONLY.keys())
else:
    faculty_leaders = (
        FACULTY_LEADERS_TO_RUN
        if FACULTY_LEADERS_TO_RUN is not None
        else sorted(MasterTutor['Faculty Leader'].dropna().unique().tolist())
    )
    faculty_leaders = [fl for fl in faculty_leaders if fl not in EXCLUDED_FACULTY_LEADERS]

# =============================================================================
# HELPERS
# =============================================================================

FILES_CONTAINER_SELECTOR = (
    '#appRoot > div.Files.sp-App-root.has-footer.is-active.od-userSelect--enabled.'
    'sp-WebViewList-enable.sp-fullHeightLayouts > div.sp-App-bodyContainer > '
    'div.sp-App-body > div > div.Files-main > div.Files-mainColumn > '
    'div.Files-contentAreaFlexContainer > div > div > div'
)

DEAD_SESSION_MARKERS = (
    'Connection refused',
    'Max retries exceeded',
    'invalid session id',
    'session deleted',
    'chrome not reachable',
    'disconnected: not connected',
)


def is_dead_session_error(e):
    """True if this exception means the browser/driver connection itself is
    gone (crashed, killed, etc.) rather than a normal page-content error."""
    text = str(e)
    return any(marker in text for marker in DEAD_SESSION_MARKERS)


def make_driver():
    """Creates one independent Chrome instance. Each parallel Faculty Leader
    worker gets its own -- no shared browser state between them.

    Note: uses a PERSISTENT profile (PERSISTENT_PROFILE_DIR), not a fresh
    one -- see that constant's comment. Since MAX_PARALLEL_LEADERS is 1,
    only one Chrome instance ever has this profile open at a time, so
    reusing the same directory across runs (and across leaders) is safe."""
    chrome_options = Options()
    chrome_options.add_argument(f'--user-data-dir={PERSISTENT_PROFILE_DIR}')
    chrome_options.add_experimental_option("prefs", {
        "download.default_directory": downloads_path,
        "download.prompt_for_download": False,
        "directory_upgrade": True
    })
    if HEADLESS:
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--window-size=1920,1080')
    chrome_options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    # Force a large virtual window size regardless of the physical display --
    # a smaller viewport makes SharePoint collapse its breadcrumb bar into a
    # "..." overflow menu, which breaks element lookups by title/text.
    driver.set_window_size(1920, 1080)
    if not HEADLESS:
        driver.maximize_window()

    try:
        driver.execute_cdp_cmd('Page.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': downloads_path
        })
    except Exception:
        pass

    return driver


def safe_click(driver, element):
    """Regular .click() fails with ElementClickInterceptedException when a
    SharePoint hover popover sits on top of the target element. Press
    Escape to dismiss any such popover, then fall back to a JS click if the
    normal click still gets intercepted."""
    try:
        element.click()
    except Exception:
        try:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.5)
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)


def find_span_element(driver, text, max_scrolls=200, pause=0.4):
    """SharePoint's file/folder list is virtualized -- an item's <span> only
    exists in the DOM once you've scrolled near it."""
    container = driver.find_element(By.CSS_SELECTOR, FILES_CONTAINER_SELECTOR)
    xpath = '//span[text()="{}"]'.format(text)
    last_height = None
    for _ in range(max_scrolls):
        elements = driver.find_elements(By.XPATH, xpath)
        if elements:
            return elements[0]
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollTop + arguments[0].clientHeight;", container
        )
        time.sleep(pause)
        new_height = driver.execute_script("return arguments[0].scrollHeight;", container)
        if new_height == last_height:
            time.sleep(1)
            elements = driver.find_elements(By.XPATH, xpath)
            if elements:
                return elements[0]
            break
        last_height = new_height
    raise NoSuchElementException('Could not locate "{}" after scrolling through the list.'.format(text))


NAME_HEADER_XPATH = (
    "/html/body/div[4]/div/div/div/div[2]/div/div[2]/main/div/div/div/div/div/div/div/div/div/div[1]/div/div[4]"
)


def sort_name_descending(driver):
    name_header = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, NAME_HEADER_XPATH))
    )
    safe_click(driver, name_header)
    descending_button = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, "//span[contains(text(),'Z to A')]"))
    )
    safe_click(driver, descending_button)
    print("Sorted by descending. Retrying folder find...")
    time.sleep(3)


def find_span_element_with_sort_fallback(driver, text, max_scrolls=200, pause=0.4):
    try:
        return find_span_element(driver, text, max_scrolls=max_scrolls, pause=pause)
    except NoSuchElementException as original_error:
        if not ENABLE_SORT_FALLBACK:
            raise
        try:
            sort_name_descending(driver)
        except Exception as sort_error:
            print(f'Could not sort Z-A ({sort_error}) -- re-raising original error.')
            raise original_error
        return find_span_element(driver, text, max_scrolls=max_scrolls, pause=pause)


def find_first_matching_span(driver, candidate_texts):
    last_error = None
    for text in candidate_texts:
        try:
            return find_span_element_with_sort_fallback(driver, text)
        except NoSuchElementException as e:
            last_error = e
    raise last_error


def unicode_variants(s):
    """Returns [s, NFC-normalized, NFD-normalized], deduped. Guards against
    accented characters (e.g. the 'e' in "Monet") being stored as a
    different, visually-identical Unicode form in Master_Tutor.csv vs. the
    literal SharePoint file/folder name -- XPath text() matching is an
    exact string comparison and silently treats these as NOT equal."""
    variants = [s, unicodedata.normalize('NFC', s), unicodedata.normalize('NFD', s)]
    return list(dict.fromkeys(variants))


def normalize_name(s):
    """Normalizes punctuation that commonly differs between the CSV,
    SharePoint folder names, and the metrics sheet."""
    if s is None:
        return ''
    s = s.replace('’', "'").replace('‘', "'")
    s = s.replace('–', '-').replace('—', '-')
    return ' '.join(s.split()).strip().lower()


# Tutors whose SharePoint folder name doesn't match their metrics-sheet /
# KPI-file name in ways that go beyond punctuation (maiden names, nicknames).
NAME_ALIASES = {
    'Joyraj Dsouza': "Joyraj D'souza",
    'Yvrine Nguiwenga Nketcha': 'Yvrine Nguiwenga-Nketcha',
    'Daniel Esquivel Reynoso': 'Daniel Esquivel-Reynoso',
    'Shaun ONeil': "Shaun O'Neil",
    'Cara Rabaev': "Cara Rabaev (McLaughlin)",
    'Jakob ONeal': "Jakob O'Neal",
    'Nayley Rolon Gomez': "Nayley Rolon-Gomez",
    # Master_Tutor.csv spells her "Nayely"; the SharePoint file itself is
    # spelled "Nayley" -- confirmed after the file-search (not folder-
    # search) failed for her specifically.
    'Nayely Rolon-Gomez': 'Nayley Rolon-Gomez',
}


def find_tutor_row(metrics_sheet, tutor):
    """Looks for the tutor in the metrics sheet, trying the plain name, its
    known alias, and normalized-punctuation matching. Returns
    (row, resolved_name) or (None, tutor). Locked because multiple
    leader-threads read the same metrics_sheet concurrently."""
    candidates = [tutor]
    if tutor in NAME_ALIASES:
        candidates.append(NAME_ALIASES[tutor])
    normalized_candidates = [normalize_name(c) for c in candidates]

    with METRICS_LOCK:
        for row in metrics_sheet.iter_rows(min_row=1, max_row=metrics_sheet.max_row,
                                            min_col=1, max_col=metrics_sheet.max_column):
            for cell in row:
                if cell.value and isinstance(cell.value, str):
                    normalized_cell = normalize_name(cell.value)
                    for candidate, norm_candidate in zip(candidates, normalized_candidates):
                        if norm_candidate in normalized_cell:
                            return row, candidate
    return None, tutor


def enter_team_folder(driver, il_team_folders_url, team_folder_name):
    """(Re)navigates from the top-level IL Team Folders listing into a
    specific Faculty Leader's team folder and returns the resulting URL."""
    driver.get(il_team_folders_url)
    time.sleep(3)
    team_button = find_span_element_with_sort_fallback(driver, team_folder_name)
    safe_click(driver, team_button)
    time.sleep(3)
    return driver.current_url


def find_kpis_sheet(workbook, tutor_display_name):
    """Looks up the 'KPIs' sheet tolerant of stray whitespace/case (e.g. a
    tracker whose sheet is literally named "KPIs " with a trailing space --
    confirmed case: Alexa Pinera's file). Exact match wins if present;
    otherwise falls back to a whitespace/case-insensitive match. Explicitly
    excludes 'KPIs (Old)', which exists in every new-format tracker
    alongside the real one."""
    if 'KPIs' in workbook.sheetnames:
        return workbook['KPIs']
    for name in workbook.sheetnames:
        if name.strip().lower() == 'kpis':
            print(f'Note: "{tutor_display_name}" tracker has a KPIs sheet named "{name}" (not exactly "KPIs") -- using it anyway.')
            return workbook[name]
    raise KeyError(
        f'No "KPIs" sheet found for "{tutor_display_name}" (sheets present: {workbook.sheetnames})'
    )


def save_debug_screenshot(driver, tag):
    try:
        path = os.path.join(error_screenshot_dir, '{}_{}.png'.format(tag, int(time.time())))
        driver.save_screenshot(path)
        print(f'Saved debug screenshot: {path}')
    except Exception as e:
        print(f'Could not save debug screenshot: {e}')


def reupload_file_to_current_folder(driver, local_file_path):
    """Uploads local_file_path into whichever SharePoint folder is currently
    open, confirming the "already exists -- replace?" prompt. Gated behind
    ENABLE_REUPLOAD (off by default -- see the note at the top of CONFIG)."""
    try:
        file_input = driver.find_element(By.CSS_SELECTOR, "input[type='file']")
    except NoSuchElementException:
        file_input = None

    if file_input is None:
        upload_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Create or upload']]"))
        )
        safe_click(driver, upload_button)
        time.sleep(1)
        try:
            file_input = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
            )
        finally:
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.5)

    driver.execute_script(
        "arguments[0].style.display='block'; arguments[0].style.visibility='visible'; "
        "arguments[0].style.opacity=1; arguments[0].style.height='1px'; arguments[0].style.width='1px';",
        file_input
    )
    file_input.send_keys(local_file_path)

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, '//div[contains(@class, "toastInnerContainer")]'))
        )
        replace_button = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, '//button[@name="Replace"]'))
        )
        safe_click(driver, replace_button)
    except Exception:
        pass

    time.sleep(3)


def process_tutor(driver, tutor, team_folder_url, metrics_sheet, il_team_folders_url, team_folder_name,
                   faculty_leader, max_attempts=MAX_TUTOR_ATTEMPTS):
    """Downloads a tutor's KPI Tracker, appends their metrics row, and saves
    it into Desktop/KPI_Trackers/<faculty_leader>/. Retries up to
    max_attempts times, doing a hard reset between attempts.
    Returns (success, current_team_folder_url, reason, reupload_ok).

    `tutor` should be the ORIGINAL name as it appears in Master_Tutor.csv
    (real apostrophes/dashes intact) -- the folder-name search below tries
    several spelling variants itself instead of the caller pre-stripping
    punctuation, since the actual SharePoint folder name doesn't always
    match any single one of those forms (confirmed case: "Conifer
    O'Sullivan"'s folder didn't match the apostrophe-stripped form phase 1
    used to search for exclusively)."""
    folder_name_candidates = list(dict.fromkeys(
        variant
        for base in [
            tutor,
            NAME_ALIASES.get(tutor, tutor),
            tutor.replace("'", "").replace("’", "").replace("-", " "),
        ]
        for variant in unicode_variants(base)
    ))

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            time.sleep(2)
            general_button = find_first_matching_span(driver, folder_name_candidates)
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", general_button
            )
            time.sleep(3)
            general_button = find_first_matching_span(driver, folder_name_candidates)  # re-fetch, avoid staleness
            safe_click(driver, general_button)

            tutor_name_info, resolved_name = find_tutor_row(metrics_sheet, tutor)
            if tutor_name_info is None:
                print(f'Tutor name not found in the Metrics file for "{tutor}"!')
                driver.get(team_folder_url)
                time.sleep(3)
                return False, team_folder_url, 'not found in Metrics file', None

            time.sleep(5)
            safe_click(driver, driver.find_element(By.XPATH, '//span[text()="KPI Tracker"]'))
            time.sleep(5)

            # Reuse the same broadened pool as the folder search (aliases +
            # punctuation-stripped + Unicode NFC/NFD variants), plus
            # resolved_name -- the file name doesn't always match whichever
            # single form matched the folder (confirmed case: "Nayely" vs.
            # "Nayley" Rolon-Gomez -- folder matched one spelling, the
            # actual .xlsx file uses the other).
            file_name_candidates = list(dict.fromkeys(
                [resolved_name] + folder_name_candidates + unicode_variants(resolved_name)
            ))
            file_button = find_first_matching_span(
                driver, ['{} KPI Tracker.xlsx'.format(c) for c in file_name_candidates]
            )
            ActionChains(driver).context_click(file_button).perform()
            time.sleep(3)
            safe_click(driver, driver.find_element(By.XPATH, "//span[text()='Download']"))
            time.sleep(5)

            kpi_file = None
            for filename in os.listdir(downloads_path):
                if filename.endswith('.xlsx') and any(
                    '{} KPI Tracker'.format(c) in filename for c in file_name_candidates
                ):
                    kpi_file = os.path.join(downloads_path, filename)
                    break

            if kpi_file is None:
                raise FileNotFoundError(f'KPI Tracker file not found for "{resolved_name}"')

            kpi_workbook = openpyxl.load_workbook(kpi_file)
            kpi_sheet = find_kpis_sheet(kpi_workbook, resolved_name)
            next_row = None
            for row in range(1, kpi_sheet.max_row + 1):
                if not kpi_sheet.cell(row=row, column=1).value:
                    next_row = row
                    break
            if not next_row:
                next_row = kpi_sheet.max_row + 1

            # Copy the number format along with the value -- otherwise a
            # percentage like 0.675 pastes in as the raw decimal "0.675"
            # instead of displaying as "68%" the way the existing rows do.
            for col_index, cell in enumerate(tutor_name_info, start=1):
                new_cell = kpi_sheet.cell(row=next_row, column=col_index, value=cell.value)
                new_cell.number_format = cell.number_format

            kpi_workbook.save(os.path.abspath(kpi_file))

            leader_dir = os.path.join(output_base_dir, faculty_leader.replace('/', '-'))
            os.makedirs(leader_dir, exist_ok=True)
            destination = os.path.join(leader_dir, os.path.basename(kpi_file))
            shutil.move(kpi_file, destination)

            print(f"Tutor row has been copied to the KPI Tracker and saved for {resolved_name} -> {destination}")

            reupload_ok = None
            if ENABLE_REUPLOAD:
                reupload_ok = True
                try:
                    reupload_file_to_current_folder(driver, destination)
                    print(f"Re-uploaded updated tracker to SharePoint for {resolved_name}.")
                except Exception as upload_error:
                    reupload_ok = False
                    print(f'Local file saved, but re-upload to SharePoint failed for "{tutor}": {upload_error}')
                    if is_dead_session_error(upload_error):
                        raise
                    save_debug_screenshot(driver, 'reupload_{}'.format(tutor.replace(' ', '_')))

            driver.get(team_folder_url)
            time.sleep(3)
            return True, team_folder_url, None, reupload_ok

        except Exception as e:
            last_error = e
            print(f'Attempt {attempt}/{max_attempts} failed for tutor "{tutor}": {e}')
            if is_dead_session_error(e):
                print(f'Browser session appears to have crashed for tutor "{tutor}" -- not retrying against a dead session.')
                return False, team_folder_url, 'browser session crashed', None
            if attempt < max_attempts:
                backoff = 5 * attempt
                print(f'Waiting {backoff}s, then re-entering the team folder from scratch before retrying...')
                time.sleep(backoff)
                try:
                    team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
                except Exception as reset_error:
                    print(f'Hard reset failed too: {reset_error}')

    save_debug_screenshot(driver, tutor.replace(' ', '_'))
    print(f'Giving up on tutor "{tutor}" after {max_attempts} attempts. Last error: {last_error}')
    return False, team_folder_url, str(last_error).splitlines()[0] if last_error else 'unknown error', None


def click_primary_login_button(driver, timeout=20):
    """id="idSIButton9" is Microsoft's primary-action button ID, and it is
    REUSED across multiple different steps of this login wizard -- "Next"
    on the email page, "Sign in" on the password page, AND "Yes" on "Stay
    signed in?" all share this exact same ID. That's a real, confirmed
    problem, not a theoretical one: a screenshot caught a run stuck on the
    password page with the password typed in but never submitted, while a
    sibling thread's click on "idSIButton9" raised a stale element error --
    consistent with the click landing on the password page's button right
    as it was being replaced by the next step's (same-ID) button. Retries
    on a stale reference instead of giving up immediately."""
    last_error = None
    for _ in range(3):
        try:
            button = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.ID, 'idSIButton9'))
            )
            button.click()
            return
        except StaleElementReferenceException as e:
            last_error = e
            time.sleep(1)
            continue
    raise last_error


def attempt_sign_in(driver, max_steps=8):
    """Signs in by detecting whatever screen is ACTUALLY on-screen at each
    step and reacting to it, instead of assuming a fixed email -> password
    -> KMSI sequence. That rigid sequence broke the moment the browser
    switched to a persistent profile (PERSISTENT_PROFILE_DIR): with an
    account already cached, Microsoft shows an account-picker tile instead
    of a blank email field, and depending on what's remembered it may skip
    straight past password entry too. Loops, re-reading page state each
    time, until it reaches "General" (logged in) or runs out of steps."""
    driver.get("https://revprepllc.sharepoint.com/sites/FacultyLeadership/Shared%20Documents")

    for step in range(max_steps):
        if driver.find_elements(By.XPATH, '//span[text()="General"]'):
            return  # already logged in

        # Account picker (persistent-profile case -- Microsoft already has
        # this account cached). Gated behind the page's own "Pick an
        # account" heading -- the bare text match on LOGIN_EMAIL alone
        # (without this gate) ALSO matches the small email breadcrumb
        # ("tyler.harrington@revolutionprep.com <-") shown above the
        # password field on the *password* page, which sent the loop
        # back to the email step in an endless loop instead of ever
        # reaching the password field (confirmed via screenshot).
        on_account_picker = driver.find_elements(By.XPATH, '//*[contains(text(),"Pick an account")]')
        if on_account_picker:
            tiles = driver.find_elements(By.XPATH, f'//*[normalize-space(text())="{LOGIN_EMAIL}"]')
            if tiles:
                safe_click(driver, tiles[0])
                print(f'Clicked cached account tile for {LOGIN_EMAIL}.')
                time.sleep(2)
                continue
            use_another = driver.find_elements(By.XPATH, '//*[contains(text(),"Use another account")]')
            if use_another:
                safe_click(driver, use_another[0])
                time.sleep(2)
                continue

        # Blank email-entry field.
        email_fields = [e for e in driver.find_elements(By.ID, 'i0116') if e.is_displayed()]
        if email_fields:
            email_fields[0].send_keys(LOGIN_EMAIL)
            click_primary_login_button(driver)
            time.sleep(2)
            continue

        # Password field (may or may not appear, depending on what the
        # persistent profile already remembers).
        password_fields = [e for e in driver.find_elements(By.ID, 'i0118') if e.is_displayed()]
        if password_fields:
            password_fields[0].send_keys(LOGIN_PASSWORD)
            click_primary_login_button(driver)
            time.sleep(2)
            continue

        # "Stay signed in?" -- reuses idSIButton9's ID, so it's matched by
        # its own heading text, not the button ID, to avoid confusion with
        # the password page's "Sign in" button.
        if driver.find_elements(By.XPATH, '//*[text()="Stay signed in?"]'):
            click_primary_login_button(driver, timeout=10)
            print('Clicked "Stay signed in?" -> Yes.')
            time.sleep(2)
            continue

        # Nothing recognized yet -- page may still be transitioning
        # between steps. Wait a beat and re-read state.
        time.sleep(2)

    print(f'attempt_sign_in: did not recognize a way forward after {max_steps} steps -- '
          f'login_and_reach_il_team_folders will catch/report it if this actually failed.')


def login_and_reach_il_team_folders(driver):
    attempt_sign_in(driver)

    general_button = None
    last_error = None
    for attempt in range(1, 4):
        try:
            general_button = WebDriverWait(driver, 40).until(
                EC.element_to_be_clickable((By.XPATH, '//span[text()="General"]'))
            )
            break
        except Exception as e:
            last_error = e
            if attempt < 3:
                print(f'Timed out waiting for "General" -- redoing sign-in and trying again ({attempt}/2)...')
                save_debug_screenshot(driver, f'login_general_timeout_attempt{attempt}')
                print(f'Was stuck on: {driver.current_url}')
                attempt_sign_in(driver)
    if general_button is None:
        save_debug_screenshot(driver, 'login_general_timeout_final')
        print(f'Stuck on: {driver.current_url}')
        raise NoSuchElementException(
            f'Timed out waiting for "General" even after redoing sign-in twice. Last error: {last_error}'
        )
    safe_click(driver, general_button)

    il_team_folders_button = WebDriverWait(driver, 40).until(
        EC.element_to_be_clickable((By.XPATH, '//span[text()="IL Team Folders"]'))
    )
    safe_click(driver, il_team_folders_button)
    time.sleep(2)

    return driver.current_url


def run_faculty_leader(faculty_leader, start_delay=0):
    """Everything needed to process one Faculty Leader end-to-end, in its
    own Chrome instance. Runs inside a worker thread; returns
    (failures, reupload_warnings)."""
    if start_delay:
        time.sleep(start_delay)

    print(f'[{faculty_leader}] starting...')
    local_failures = []
    local_reupload_warnings = []
    driver = None
    try:
        driver = make_driver()
        il_team_folders_url = login_and_reach_il_team_folders(driver)
        team_folder_name = "{}'s Team".format(faculty_leader.split()[0])

        try:
            team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
        except NoSuchElementException:
            print(f'[{faculty_leader}] Could not find team folder "{team_folder_name}" -- skipping.')
            return [(faculty_leader, '(entire team)', 'team folder not found')], []

        tutor_list = (
            MasterTutor[MasterTutor['Faculty Leader'] == faculty_leader]['Full Name']
            .dropna()
            .tolist()
        )
        if RETRY_ONLY is not None:
            wanted = set(RETRY_ONLY.get(faculty_leader, []))
            tutor_list = [t for t in tutor_list if t in wanted]
            print(f'[{faculty_leader}] RETRY_ONLY set -- running just {len(tutor_list)} tutor(s): {tutor_list}')

        consecutive_failures = 0
        for tutor in tutor_list:
            print(f'[{faculty_leader}] {tutor}')
            # Pass the original name (real apostrophes/dashes intact) --
            # process_tutor builds its own candidate spellings internally
            # for the folder search now, instead of only ever trying one
            # pre-stripped form.
            success, team_folder_url, reason, reupload_ok = process_tutor(
                driver, tutor, team_folder_url, metrics_sheet, il_team_folders_url, team_folder_name,
                faculty_leader
            )

            if success:
                consecutive_failures = 0
                if reupload_ok is False:
                    local_reupload_warnings.append((faculty_leader, tutor))
            else:
                local_failures.append((faculty_leader, tutor, reason))

                if reason == 'browser session crashed':
                    print(f'[{faculty_leader}] Restarting the browser after a crash...')
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    try:
                        driver = make_driver()
                        il_team_folders_url = login_and_reach_il_team_folders(driver)
                        team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
                        print(f'[{faculty_leader}] Browser restarted successfully, continuing...')
                        consecutive_failures = 0
                        continue
                    except Exception as restart_error:
                        print(
                            f'[{faculty_leader}] Could not restart the browser ({restart_error}) -- '
                            f'marking all remaining tutors for this leader as not attempted.'
                        )
                        remaining = tutor_list[tutor_list.index(tutor) + 1:]
                        for remaining_tutor in remaining:
                            local_failures.append(
                                (faculty_leader, remaining_tutor, 'not attempted -- browser restart failed')
                            )
                        break

                consecutive_failures += 1
                if consecutive_failures >= CONSECUTIVE_FAILURE_COOLDOWN_THRESHOLD:
                    print(
                        f'[{faculty_leader}] {consecutive_failures} tutors in a row failed completely -- '
                        f'cooling down {COOLDOWN_SECONDS}s...'
                    )
                    time.sleep(COOLDOWN_SECONDS)
                    try:
                        team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
                    except Exception as e:
                        print(f'[{faculty_leader}] Could not re-enter team folder after cooldown: {e}')
                    consecutive_failures = 0

    except Exception as e:
        print(f'[{faculty_leader}] Fatal error, aborting this leader: {e}')
        local_failures.append((faculty_leader, '(entire team)', f'fatal error: {e}'))
    finally:
        if driver is not None:
            driver.quit()
        print(f'[{faculty_leader}] done.')

    return local_failures, local_reupload_warnings


def main():
    worker_count = MAX_PARALLEL_LEADERS if MAX_PARALLEL_LEADERS else len(faculty_leaders)

    all_failed_tutors = []
    all_reupload_warnings = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            # Wider stagger (was 2s) -- multiple leaders logging in at once
            # hits SharePoint's SSO login concurrently and times out, which
            # looks like a code bug but is really login contention. Phase 2
            # already fixed this; matching it here.
            executor.submit(run_faculty_leader, fl, i * 15): fl
            for i, fl in enumerate(faculty_leaders)
        }
        for future in as_completed(futures):
            fl = futures[future]
            try:
                failures, reupload_warnings = future.result()
                all_failed_tutors.extend(failures)
                all_reupload_warnings.extend(reupload_warnings)
            except Exception as e:
                print(f'[{fl}] worker crashed: {e}')
                all_failed_tutors.append((fl, '(entire team)', f'worker crashed: {e}'))

    print('All Faculty Leaders processed.')

    print()
    print('=' * 60)
    if all_failed_tutors:
        print(f'{len(all_failed_tutors)} tutor(s) did NOT get processed successfully:')
        print()
        current_leader = None
        for leader, tutor, reason in sorted(all_failed_tutors, key=lambda x: x[0]):
            if leader != current_leader:
                print(f'{leader}:')
                current_leader = leader
            print(f'  - {tutor}  ({reason})')
        print()
        print('Debug screenshots for these are in: {}'.format(error_screenshot_dir))
    else:
        print('Every tutor was processed successfully. No failures to report.')
    print('=' * 60)

    if all_reupload_warnings:
        print()
        print('=' * 60)
        print(
            f'{len(all_reupload_warnings)} tutor(s) were downloaded/updated/saved locally fine, but the automatic '
            f're-upload back to SharePoint failed (or was never attempted -- ENABLE_REUPLOAD={ENABLE_REUPLOAD}) -- '
            f'these still need to be uploaded to their KPI Tracker folder by hand:'
        )
        print()
        current_leader = None
        for leader, tutor in sorted(all_reupload_warnings, key=lambda x: x[0]):
            if leader != current_leader:
                print(f'{leader}:')
                current_leader = leader
            print(f'  - {tutor}')
        print('=' * 60)

    if not ENABLE_REUPLOAD:
        print()
        print('=' * 60)
        print(
            'Note: automatic re-upload to SharePoint is currently DISABLED '
            '(ENABLE_REUPLOAD = False) after it repeatedly crashed the browser. '
            'All finished trackers were only saved locally -- upload the folders '
            'in Desktop/KPI_Trackers/<Faculty Leader>/ to SharePoint by hand, '
            'then run kpi_tracker_phase2_copy_from_staging.py.'
        )
        print('=' * 60)


if __name__ == "__main__":
    main()
