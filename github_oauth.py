from flask import Flask, request
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask('app')

@app.route('/')
def home():
  return "Hello, World!"

@app.route('/callback')
def callback():
  code = request.args.get('code')
  client_id = os.getenv('GITHUB_CLIENT_ID')
  client_secret = os.getenv('GITHUB_CLIENT_SECRET')
  data = {
    'client_id': client_id,
    'client_secret': client_secret,
    'code': code
  }
  response = requests.post(
    'https://github.com/login/oauth/access_token', data=data, headers={'Accept': 'application/json'})
  access_token = response.json().get('access_token')
  # Here you would store the access token in a database or use it to take action for the user
  print(access_token)  # This will print the access token to the console
  print('success')
  print(response.json())
  return "Authenticated successfully"
  

if __name__ == '__main__':
  app.run(host='0.0.0.0', port=8080)

# need access token with public repo permission
# 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
# curl.exe -X PUT -H "Accept: application/vnd.github.v3+json" -H "Authorization: Bearer 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" https://api.github.com/user/starred/Berry-13/LibreChat-DiscordBot

#curl.exe -X PUT -H "Accept: application/vnd.github.v3+json" -H "Authorization: Bearer ghu_niJ8KRzNY6LN6vJhcPw6tLmcQ9nbir3BWog0" https://api.github.com/user/starred/Berry-13/LibreChat-DiscordBot


curl -H "Authorization: Bearer OAUTH-TOKEN" https://api.github.com/users/codertocat -I
HTTP/2 200
X-OAuth-Scopes: repo, user
X-Accepted-OAuth-Scopes: user