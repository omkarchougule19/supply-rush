# Blueprint: Securing the Instructor Dashboard

> [!NOTE]
> **Status: Implemented.** This blueprint's approach (HTTP Basic Auth, env-var
> credentials, zero DB tables) was built as described below, as part of a
> larger access-control feature that also added passwordless student email
> verification. See [rules.txt](rules.txt) Section 8 (Access Control) and the
> Development Log for the full design and implementation record. This file is
> kept for historical reference.

This document provides a detailed, step-by-step implementation guide to secure the Instructor Dashboard. It is designed so that any future developer or AI agent can pick up this task and implement it with minimal context.

---

## 1. Selected Approach: HTTP Basic Authentication
For a small team (maximum 3 users), database-driven authentication is unnecessary overhead. We will use **HTTP Basic Authentication** with credentials loaded directly from environment variables.
- **Why?** It requires zero frontend changes. The browser natively displays a login credentials popup when attempting to access the page and handles header injection/caching automatically.
- **No Database Tables**: No migrations, backups, or account tables needed.

---

## 2. Step-by-Step Implementation Plan

### Step 1: Update Environment Configurations
Add credential variables to both the `.env` and `.env.example` files:
```ini
# comma-separated list of allowed instructor usernames
INSTRUCTOR_USERNAMES=admin,instructor1,instructor2

# comma-separated list of matching passwords (order-dependent)
INSTRUCTOR_PASSWORDS=securepass1,securepass2,securepass3
```

### Step 2: Implement the Security Dependency
In [routes.py](file:///d:/projectRA/version4/supply-rush/routes.py) (or a new `auth.py` helper), define the authentication helper using FastAPI's `HTTPBasic` security scheme:

```python
import os
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()

def authenticate_instructor(credentials: HTTPBasicCredentials = Depends(security)):
    # Load allowed users from env (fallback to safe defaults in dev if missing)
    allowed_users = os.getenv("INSTRUCTOR_USERNAMES", "admin").split(",")
    allowed_passwords = os.getenv("INSTRUCTOR_PASSWORDS", "password").split(",")
    
    # Map usernames to passwords
    user_map = dict(zip(allowed_users, allowed_passwords))
    
    correct_password = user_map.get(credentials.username)
    if not correct_password or credentials.password != correct_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            # This header is critical! It instructs the browser to show the login popup
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
```

### Step 3: Secure the HTML Dashboard Route
Locate where the `/instructor` static route is served (usually in `main.py` or where static files are defined) and apply the `Depends(authenticate_instructor)` dependency:

```python
from routes import authenticate_instructor

@app.get("/instructor", dependencies=[Depends(authenticate_instructor)])
def get_instructor_dashboard():
    # Return your static instructor_dashboard.html file
    # Example:
    # return FileResponse("static/instructor_dashboard.html")
    pass
```

### Step 4: Secure the Instructor API Endpoints
Update all instructor endpoints in [routes.py](file:///d:/projectRA/version4/supply-rush/routes.py) to require this dependency:

```python
@router.post("/scenarios", response_model=ScenarioOut, tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def create_scenario(payload: ScenarioCreate, db: Session = Depends(get_db)):
    # Existing creation logic...
    pass

@router.get("/scenarios", response_model=List[ScenarioOut], tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def list_scenarios(db: Session = Depends(get_db)):
    # Existing listing logic...
    pass

@router.delete("/scenarios/{code}", tags=["instructor"], dependencies=[Depends(authenticate_instructor)])
def delete_scenario(code: str, db: Session = Depends(get_db)):
    # Existing deletion logic...
    pass
```

---

## 3. Verification Plan

1. **Test Unauthorized Access**:
   - Open a private browsing session.
   - Navigate to `http://localhost:8000/instructor`.
   - Verify that the browser displays a username and password dialog, blocking access to the page.
   
2. **Test Valid Credentials**:
   - Enter one of the username/password pairs defined in the `.env` file.
   - Verify that the dashboard loads successfully.
   - Perform an action (e.g., create a scenario) and verify that the API requests pass authorization without throwing `401 Unauthorized` errors.

3. **Test Invalid Credentials**:
   - Enter incorrect credentials.
   - Verify that the browser rejects the login and prompts again, preventing the page/APIs from executing.
