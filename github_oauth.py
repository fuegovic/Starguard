from flask import Flask, request, render_template, redirect, url_for
import os
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from authlib.integrations.flask_client import OAuthError

load_dotenv()

app = Flask('app')
app.template_folder = 'html'
app.secret_key = os.getenv('SECRET_KEY')

oauth = OAuth(app)

github = oauth.register(
    name='github',
    client_id=os.getenv('GITHUB_CLIENT_ID'),
    client_secret=os.getenv('GITHUB_CLIENT_SECRET'),
    authorize_url='https://github.com/login/oauth/authorize',
    access_token_url='https://github.com/login/oauth/access_token',
    api_base_url='https://api.github.com/',
    client_kwargs={'scope': 'user:email repo'}  # Add scope parameter here
)

@app.route('/login')
def login():
    redirect_uri = url_for('authorize', _external=True)
    return github.authorize_redirect(redirect_uri)

@app.route('/authorize')
def authorize():
    try:
        token = github.authorize_access_token()
        if token:
            # auth successful
            message = "Authentication successful!"
            print(f"Auth Status: {message}")
            
            user_resp = github.get('user', token=token)
            username = user_resp.json()['login']
            print(f"User's username: {username}")

            resp = github.get('user/emails', token=token)
            email = resp.json()[0]['email']
            print(f"User's email: {email}")
            print(f"token: {token}")


            return render_template('valid.html')
        else:
            # auth failed
            message = "Invalid link!"
            print(f"Auth Status: {message}")

    except OAuthError as e:
        # Handle OAuth errors here
        message = f"OAuth error: {e.description}"
        print(f"Auth Status: {message}")
    
    return render_template('invalidLink.html')

@app.route('/')
def home():
    return render_template('home.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)