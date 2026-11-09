#!/usr/bin/env python3
"""
diagnose_team_list.py -- READ-ONLY diagnostic (clicks nothing but folder
navigation, downloads nothing, edits nothing).

Opens Annelies's Team folder, scrolls the list step by step printing what the
list container is doing, prints every folder name that actually appears, then
tries to open Jessica Peterson's folder directly by URL. Saves screenshots to
Desktop/automation_errors/. Paste the console output back.

Run:  python3 diagnose_team_list.py
"""
import os
import time

from selenium.webdriver.common.by import By

import kpi_tracker_phase1_download_update as p

TEAM = "Annelies's Team"
TEST_TUTOR = "Jessica Peterson"


def shot(driver, tag):
    path = os.path.join(p.error_screenshot_dir, f"diag_{tag}.png")
    driver.save_screenshot(path)
    print(f"  screenshot -> {path}")


def main():
    driver = p.make_driver()
    try:
        il_url = p.login_and_reach_il_team_folders(driver)
        team_url = p.enter_team_folder(driver, il_url, TEAM)
        print("TEAM URL:", team_url)
        time.sleep(3)

        container = driver.find_element(By.CSS_SELECTOR, p.FILES_CONTAINER_SELECTOR)
        scrollers = driver.execute_script("""
            var out=[]; document.querySelectorAll('*').forEach(function(e){
              var cs=getComputedStyle(e);
              if(e.scrollHeight>e.clientHeight+20 && /(auto|scroll)/.test(cs.overflowY)){
                out.push(e.tagName+'.'+String(e.className).slice(0,60)+' sh='+e.scrollHeight+' ch='+e.clientHeight);
              }});
            return out.slice(0,15);""")
        print("SCROLLABLE ELEMENTS:", scrollers)
        seen = []
        for i in range(40):
            names = [e.text for e in driver.find_elements(By.XPATH, "//div[@role='row']//span[@class and string-length(text())>0][1]")]
            top, sh, ch = driver.execute_script(
                "var c=arguments[0];return [c.scrollTop,c.scrollHeight,c.clientHeight];", container)
            rows = len(driver.find_elements(By.XPATH, "//div[@role='row']"))
            print(f"step {i:02d}: scrollTop={top} scrollHeight={sh} clientHeight={ch} rows_in_dom={rows}")
            for n in names:
                if n not in seen:
                    seen.append(n)
            p._scroll_list_down(driver, container)
            time.sleep(1.5)
        shot(driver, "bottom_of_list")
        print(f"\nDistinct row texts seen while scrolling ({len(seen)}):")
        print(seen)
        print("Jessica Peterson seen:", any(TEST_TUTOR in n for n in seen))

        print("\n--- direct URL test ---")
        url = p.tutor_folder_url(team_url, TEST_TUTOR)
        print("URL:", url)
        if url:
            driver.get(url)
            time.sleep(8)
            print("landed on:", driver.current_url)
            print("has KPI Tracker span:", bool(driver.find_elements(By.XPATH, '//span[text()="KPI Tracker"]')))
            shot(driver, "direct_url_result")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
