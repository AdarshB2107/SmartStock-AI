# SmartStock AI – Demand Forecasting and Inventory Optimization

SmartStock AI is a Python-based project that generates synthetic retail data, forecasts product demand, and creates inventory replenishment recommendations. It includes a FastAPI backend and a Streamlit dashboard.

## Tech Stack

- Python
- FastAPI
- Streamlit
- SQLite
- Pandas, NumPy
- Pytest

## Installation and Setup

**1. Clone the repository**

```bash
git clone <YOUR_GITHUB_REPOSITORY_URL>
cd smartstock-ai
```

**2. Create and activate a virtual environment**

Windows:

```bash
python -m venv .venv
.venv\Scripts\activate
```

**3. Install dependencies**

```bash
python -m pip install -r requirements.txt
```

## Run the Application

**4. Start the FastAPI backend**

```bash
python -m uvicorn app.main:app --reload
```

API documentation: http://127.0.0.1:8000/docs

**5. Start the Streamlit dashboard**

Open a second terminal, activate the virtual environment, and run:

```bash
python -m streamlit run dashboard.py
```

Dashboard: http://localhost:8501

## Run Tests

```bash
python -m pytest -q
```

## Features

- Synthetic retail data generation
- Demand forecasting and evaluation
- Inventory replenishment recommendations
- Ground-truth validation
- Interactive dashboard and REST API

## Note

Start with a small dataset before attempting large-scale data generation.

## License

Add a `LICENSE` file if you intend to distribute this project under an open-source license.