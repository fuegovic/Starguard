# Discord-GitHub-bot

## To Do:
-  💫github_oauth.py: 
  - make check for star status of the specified repo (using the user's bearer tkn)
  - add db entry

- 🤖 bot.py: 
  - add db support 
  - when auth -> timeout 1min -> check if db entry was made -> update verified user status -> update role

- 😈 add "admin" command to force re-verification and update roles assignments
- 🔎 Implement a periodic check of the starred users or something like that
- 🪓 Split the user validation commands from the other github menu?
- 🖲️ Add functions to the bot (<ins>5 buttons max</ins>)
  - example:
  ![image](https://github.com/fuegovic/Discord-GH-bot/assets/32828263/86b90c99-48f4-4c13-9b96-df552b9b9466)
- 📝 Documentation, how to use and configure
- ... 👀
- ~~📃 Make `requirements.txt`~~
- ~~🐋 Put everything in a docker container for easier deployment~~
- ~~🖼️ Better page for successful login~~
- ~~🤔 Make sur the API handles multiple users using the bot at once~~
