import os
from app import web_app

if __name__ == '__main__':
    web_app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))
