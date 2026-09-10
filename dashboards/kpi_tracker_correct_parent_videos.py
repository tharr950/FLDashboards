#!/usr/bin/env python3
"""
kpi_tracker_correct_parent_videos.py

ONE-OFF CORRECTION PASS -- not the regular pipeline. The original phase 1
run for Ian Plamondon, Annelies de Groot, and Ela Cross wrote a wrong
"% Parent Updates with Videos" value into every tutor's tracker (a real
bug in pull_fl_dashboard_history.py's compute_parent_update_videos,
since fixed -- see that file's docstring). This script corrects it.

Unlike phase 1, this does NOT append a new row. For each tutor it:
  1. Re-downloads their CURRENT KPI Tracker fresh from SharePoint (never
     touches a stale local copy -- if the tutor or anyone else edited the
     file since our last pass, those edits are preserved).
  2. Finds the row previously written for this exact period, matched by
     its "Date Range" value (NOT by appending) -- so we correct the
     existing row instead of creating a duplicate.
  3. Overwrites ONLY the "% Parent Updates with Videos" cell (column 7).
     Every other cell in the row, and every other row in the file, is
     left exactly as it was.
  4. Saves locally to Desktop/KPI_Trackers_Corrected/<Faculty Leader>/.

Before running this: re-run pull_fl_dashboard_history.py,
build_tutor_metrics_file.py, and build_metrics_file_for_trackers.py so
Metrics_File.xlsx on the Desktop has the CORRECTED values.

After running this: same manual step as phase 1 -- upload the contents of
each Desktop/KPI_Trackers_Corrected/<Faculty Leader>/ folder into that
Faculty Leader's "aa_Metrics_Upload" staging folder in SharePoint, then
run kpi_tracker_phase2_copy_from_staging.py to copy each corrected file
into the tutor's real KPI Tracker folder (replacing the wrong one).
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

# Annelies and Ela done. Ian Plamondon next.
FACULTY_LEADERS_TO_RUN = ['Ian Plamondon']

EXCLUDED_FACULTY_LEADERS = ['Katherine Marino', 'Nikki Pencak']

# Already confirmed missing from the Redshift/metrics pull across every
# team -- no existing row to correct for these, skip them proactively
# instead of wasting a folder search that will just fail with "no
# existing row found."
KNOWN_DEPARTED_TUTORS = {
    'Alexandra Berrios', 'Brenna Hubnik', 'Eru Kyu', 'Jacey Clifton', 'Kai Tsung',
    'Krystal Ashley', 'Megan Hoelting', 'Miki Cornwell', 'Sonja Tomasko', 'Yvanna Ramirez',
}

# Set this to re-run only specific tutors instead of every tutor for a
# leader. Leave as None to run everyone normally. When set, ONLY the
# leaders listed here run, and ONLY the tutors listed for each one.
RETRY_ONLY = None
# Example: RETRY_ONLY = {'Ela Cross': ['Some Tutor']}

MAX_TUTOR_ATTEMPTS = 3
CONSECUTIVE_FAILURE_COOLDOWN_THRESHOLD = 3
COOLDOWN_SECONDS = 120
ENABLE_SORT_FALLBACK = False

# Concurrency was NOT actually the trigger -- confirmed by re-running with
# this at 1 and hitting the exact same "Let's keep your account secure"
# screen solo. Left at 1 anyway (still cheap insurance against the
# earlier chromedriver -9 crash risk), but the real cause is below.
MAX_PARALLEL_LEADERS = 1

# Back to headless -- the one-time verification (persistent profile) is
# done, confirmed by the account owner completing it successfully.
HEADLESS = True

# (See PERSISTENT_PROFILE_DIR below, right after desktop_path is defined,
# for the actual root-cause fix.)

desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")

# Real root cause: every run previously launched a brand new, cookie-less
# Chrome profile -- Microsoft never saw it as a "known device," and after
# today's volume of automated sign-ins, its risk engine now demands
# step-up verification for every one of them, regardless of timing (the
# account owner's own regular browser never hits this, because it already
# has a trusted-device history). Pointing Chrome at a PERSISTENT profile
# directory instead of a fresh one means that once this verification is
# cleared here ONE time, the trust cookie sticks around for every future
# run that reuses this same profile.
PERSISTENT_PROFILE_DIR = os.path.join(desktop_path, ".automation_chrome_profile")
os.makedirs(PERSISTENT_PROFILE_DIR, exist_ok=True)

downloads_path = os.path.join(os.path.expanduser("~"), "Downloads")
print(f"Downloading to: {downloads_path}")

error_screenshot_dir = os.path.join(desktop_path, "automation_errors")
os.makedirs(error_screenshot_dir, exist_ok=True)

# Deliberately a DIFFERENT folder from phase 1's output -- keeps the
# corrected files clearly separate from the original (wrong) ones.
output_base_dir = os.path.join(desktop_path, "KPI_Trackers_Corrected")
os.makedirs(output_base_dir, exist_ok=True)

MASTER_TUTOR_PATH = os.path.join(desktop_path, "Master_Tutor.csv")
MasterTutor = pd.read_csv(MASTER_TUTOR_PATH)

# Metrics_File.xlsx must already be REBUILT with the corrected
# parent_update_videos values (re-run pull_fl_dashboard_history.py ->
# build_tutor_metrics_file.py -> build_metrics_file_for_trackers.py
# first) -- this script trusts whatever's in it.
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
        f"No 'Metrics_File*.xlsx' found in {desktop_path} -- rebuild it first "
        f"(pull_fl_dashboard_history.py -> build_tutor_metrics_file.py -> "
        f"build_metrics_file_for_trackers.py)."
    )

METRICS_LOCK = threading.Lock()

if RETRY_ONLY is not None:
    faculty_leaders = list(RETRY_ONLY.keys())
else:
    faculty_leaders = [fl for fl in FACULTY_LEADERS_TO_RUN if fl not in EXCLUDED_FACULTY_LEADERS]

# =============================================================================
# HELPERS (identical to kpi_tracker_phase1_download_update.py)
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
    text = str(e)
    return any(marker in text for marker in DEAD_SESSION_MARKERS)


def make_driver():
    chrome_options = Options()
    # Persistent profile (see PERSISTENT_PROFILE_DIR) instead of a fresh
    # one each run -- lets Microsoft's "remember this device" cookie from
    # the one-time manual verification carry over to future automated runs.
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
    variants = [s, unicodedata.normalize('NFC', s), unicodedata.normalize('NFD', s)]
    return list(dict.fromkeys(variants))


def normalize_name(s):
    if s is None:
        return ''
    s = s.replace('’', "'").replace('‘', "'")
    s = s.replace('–', '-').replace('—', '-')
    return ' '.join(s.split()).strip().lower()


NAME_ALIASES = {
    'Joyraj Dsouza': "Joyraj D'souza",
    'Yvrine Nguiwenga Nketcha': 'Yvrine Nguiwenga-Nketcha',
    'Daniel Esquivel Reynoso': 'Daniel Esquivel-Reynoso',
    'Shaun ONeil': "Shaun O'Neil",
    'Cara Rabaev': "Cara Rabaev (McLaughlin)",
    'Jakob ONeal': "Jakob O'Neal",
    'Nayley Rolon Gomez': "Nayley Rolon-Gomez",
    'Nayely Rolon-Gomez': 'Nayley Rolon-Gomez',
}


def find_tutor_row(metrics_sheet, tutor):
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
    driver.get(il_team_folders_url)
    time.sleep(3)
    team_button = find_span_element_with_sort_fallback(driver, team_folder_name)
    safe_click(driver, team_button)
    time.sleep(3)
    return driver.current_url


def find_kpis_sheet(workbook, tutor_display_name):
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


# =============================================================================
# THE ACTUAL CORRECTION LOGIC
# =============================================================================

def process_tutor_correction(driver, tutor, team_folder_url, metrics_sheet, il_team_folders_url, team_folder_name,
                              faculty_leader, max_attempts=MAX_TUTOR_ATTEMPTS):
    """Re-downloads the tutor's CURRENT tracker fresh, finds the row
    previously written for this exact period (matched by Date Range, NOT
    appended), and overwrites ONLY the "% Parent Updates with Videos"
    cell (column 7). Returns (success, current_team_folder_url, reason)."""
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
            general_button = find_first_matching_span(driver, folder_name_candidates)
            safe_click(driver, general_button)

            tutor_name_info, resolved_name = find_tutor_row(metrics_sheet, tutor)
            if tutor_name_info is None:
                print(f'Tutor name not found in the (rebuilt) Metrics file for "{tutor}"!')
                driver.get(team_folder_url)
                time.sleep(3)
                return False, team_folder_url, 'not found in Metrics file'

            target_dates = tutor_name_info[1].value  # column 2 = Date Range
            corrected_value = tutor_name_info[6].value  # column 7 = % Parent Updates with Videos

            time.sleep(5)
            safe_click(driver, driver.find_element(By.XPATH, '//span[text()="KPI Tracker"]'))
            time.sleep(5)

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

            # Find the row we previously wrote for this exact period,
            # matched by Date Range (column 2) -- NOT a new row. If it's
            # not there, fail loudly instead of silently appending a
            # duplicate/wrong row.
            target_row = None
            for row in range(1, kpi_sheet.max_row + 1):
                if kpi_sheet.cell(row=row, column=2).value == target_dates:
                    target_row = row
            if target_row is None:
                os.remove(kpi_file)
                driver.get(team_folder_url)
                time.sleep(3)
                return False, team_folder_url, f'no existing row found with Date Range == "{target_dates}"'

            old_value = kpi_sheet.cell(row=target_row, column=7).value
            # Deliberately NOT touching number_format -- only the value
            # changes, the cell keeps whatever formatting it already had.
            kpi_sheet.cell(row=target_row, column=7, value=corrected_value)

            kpi_workbook.save(os.path.abspath(kpi_file))

            leader_dir = os.path.join(output_base_dir, faculty_leader.replace('/', '-'))
            os.makedirs(leader_dir, exist_ok=True)
            destination = os.path.join(leader_dir, os.path.basename(kpi_file))
            shutil.move(kpi_file, destination)

            print(
                f'Corrected row {target_row} (Date Range == "{target_dates}") for {resolved_name}: '
                f'% Parent Updates with Videos {old_value} -> {corrected_value} -> {destination}'
            )

            driver.get(team_folder_url)
            time.sleep(3)
            return True, team_folder_url, None

        except Exception as e:
            last_error = e
            print(f'Attempt {attempt}/{max_attempts} failed for tutor "{tutor}": {e}')
            if is_dead_session_error(e):
                print(f'Browser session appears to have crashed for tutor "{tutor}" -- not retrying against a dead session.')
                return False, team_folder_url, 'browser session crashed'
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
    return False, team_folder_url, str(last_error).splitlines()[0] if last_error else 'unknown error'


def click_primary_login_button(driver, timeout=20):
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

        # Gated behind "Pick an account" -- without this gate, the bare
        # LOGIN_EMAIL text match ALSO matches the small email breadcrumb
        # shown above the password field on the *password* page, which
        # sent the loop back to the email step in an endless loop instead
        # of ever reaching the password field (confirmed via screenshot).
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

        email_fields = [e for e in driver.find_elements(By.ID, 'i0116') if e.is_displayed()]
        if email_fields:
            email_fields[0].send_keys(LOGIN_EMAIL)
            click_primary_login_button(driver)
            time.sleep(2)
            continue

        password_fields = [e for e in driver.find_elements(By.ID, 'i0118') if e.is_displayed()]
        if password_fields:
            password_fields[0].send_keys(LOGIN_PASSWORD)
            click_primary_login_button(driver)
            time.sleep(2)
            continue

        if driver.find_elements(By.XPATH, '//*[text()="Stay signed in?"]'):
            click_primary_login_button(driver, timeout=10)
            print('Clicked "Stay signed in?" -> Yes.')
            time.sleep(2)
            continue

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
    if start_delay:
        time.sleep(start_delay)

    print(f'[{faculty_leader}] starting...')
    local_failures = []
    driver = None
    try:
        driver = make_driver()
        il_team_folders_url = login_and_reach_il_team_folders(driver)
        team_folder_name = "{}'s Team".format(faculty_leader.split()[0])

        try:
            team_folder_url = enter_team_folder(driver, il_team_folders_url, team_folder_name)
        except NoSuchElementException:
            print(f'[{faculty_leader}] Could not find team folder "{team_folder_name}" -- skipping.')
            return [(faculty_leader, '(entire team)', 'team folder not found')]

        tutor_list = (
            MasterTutor[MasterTutor['Faculty Leader'] == faculty_leader]['Full Name']
            .dropna()
            .tolist()
        )
        if RETRY_ONLY is not None:
            wanted = set(RETRY_ONLY.get(faculty_leader, []))
            tutor_list = [t for t in tutor_list if t in wanted]
            print(f'[{faculty_leader}] RETRY_ONLY set -- running just {len(tutor_list)} tutor(s): {tutor_list}')
        else:
            before = len(tutor_list)
            tutor_list = [t for t in tutor_list if t not in KNOWN_DEPARTED_TUTORS]
            skipped = before - len(tutor_list)
            if skipped:
                print(f'[{faculty_leader}] Skipping {skipped} known-departed tutor(s) with no row to correct.')

        consecutive_failures = 0
        for tutor in tutor_list:
            print(f'[{faculty_leader}] {tutor}')
            success, team_folder_url, reason = process_tutor_correction(
                driver, tutor, team_folder_url, metrics_sheet, il_team_folders_url, team_folder_name,
                faculty_leader
            )

            if success:
                consecutive_failures = 0
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

    return local_failures


def main():
    worker_count = MAX_PARALLEL_LEADERS if MAX_PARALLEL_LEADERS else len(faculty_leaders)

    all_failed_tutors = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(run_faculty_leader, fl, i * 15): fl
            for i, fl in enumerate(faculty_leaders)
        }
        for future in as_completed(futures):
            fl = futures[future]
            try:
                failures = future.result()
                all_failed_tutors.extend(failures)
            except Exception as e:
                print(f'[{fl}] worker crashed: {e}')
                all_failed_tutors.append((fl, '(entire team)', f'worker crashed: {e}'))

    print('All Faculty Leaders processed.')

    print()
    print('=' * 60)
    if all_failed_tutors:
        print(f'{len(all_failed_tutors)} tutor(s) did NOT get corrected successfully:')
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
        print('Every tutor was corrected successfully. No failures to report.')
    print('=' * 60)

    print()
    print('=' * 60)
    print(
        'Corrected trackers were saved locally to Desktop/KPI_Trackers_Corrected/<Faculty Leader>/. '
        'Upload them to each Faculty Leader\'s aa_Metrics_Upload staging folder in SharePoint by hand, '
        'then run kpi_tracker_phase2_copy_from_staging.py to copy them into the tutors\' real KPI Tracker '
        'folders (this will replace the file with the wrong value already there).'
    )
    print('=' * 60)


if __name__ == "__main__":
    main()
