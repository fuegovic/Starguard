from flask import Flask, request, render_template
import requests
import os
from dotenv import load_dotenv

load_dotenv()

app = Flask('app')
app.template_folder = 'html'

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

    if access_token:
        # auth successful
        message = "Authentication successful!"
        return render_template('valid.html')
    else:
        # auth failed
        message = "Invalid link!"
        return render_template('invalidLink.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
    