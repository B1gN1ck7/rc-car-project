module.exports = {
  apps: [
    {
      name: "rc-car-backend",
      script: "backend/app.py",
      interpreter: "/home/b1gn1ck7/Documents/RC_Car_Project/.venv/bin/python",
      watch: ["backend/"],
      env: {
        FLASK_ENV: "development"
      }
    },
    {
      name: "rc-car-frontend",
      script: "frontend/server.js",
      watch: ["frontend/server.js", "frontend/index.html", "frontend/style.css", "frontend/app.js"],
      env: {
        NODE_ENV: "development"
      }
    }
  ]
};
