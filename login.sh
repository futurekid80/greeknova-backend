#!/bin/bash
/opt/homebrew/bin/python3 -c "
import sys
sys.path.insert(0, '/Users/apple/optionspulse')
from dotenv import load_dotenv
load_dotenv('/Users/apple/optionspulse/.env')
from services.kite_auth import auto_login
kite = auto_login()
print('Logged in:', kite.profile()['user_name'])
"
