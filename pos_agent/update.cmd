@echo off
REM עדכון הסוכן מ-GitHub + הרצה. להריץ מתוך תיקיית pos_agent (כמנהל).
cd /d "%~dp0"
echo מוריד גרסה עדכנית...
curl -fsSL -o pos_driver.py    https://raw.githubusercontent.com/suissa1002-cyber/gm-transfers/main/pos_agent/pos_driver.py
curl -fsSL -o agent.py         https://raw.githubusercontent.com/suissa1002-cyber/gm-transfers/main/pos_agent/agent.py
curl -fsSL -o inspect_dialog.py https://raw.githubusercontent.com/suissa1002-cyber/gm-transfers/main/pos_agent/inspect_dialog.py
echo.
python agent.py
