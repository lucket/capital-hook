
---
## Capital Hook: TradingView to Capital.com Webhook Automation
Capital Hook is a powerful, self-hosted FastAPI application for algorithmic traders. It serves as a **bridge between TradingView alerts (signals, screeners, Pine Script strategies) and the Capital.com CFD/forex execution API**, enabling you to automate trade execution based on your strategies. Eliminate manual trading and harness automation to respond instantly to market opportunities!

## ✨ Features

Capital Hook provides a robust set of features designed to empower algorithmic traders with seamless automation and real-time insights:

- **Automated Trade Execution**: Automatically execute trades on your Capital.com account when triggered by alerts from TradingView. This includes signals from indicators, screener results, and complex Pine Script strategies.
- **Real-time Dashboard**:
  - **Portfolio Balance**: Monitor your Capital.com account's real-time balance directly within the application's dashboard.
  - **Live Positions**: View all trades currently in execution or managed by Capital Hook, providing immediate oversight of your active strategies.
  - **Demo & Live Mode Toggle**: Easily switch between your Capital.com demo and live accounts to test strategies risk-free or deploy them confidently to real markets.
- **Dynamic Configuration**: A dedicated configuration page allows you to tailor the webhook behavior and trade parameters without modifying code:
- **Payload Setup**: Configure custom JSON payloads from TradingView webhooks to align precisely with your trading strategy’s behavior and risk management rules.
    - **Take Profit (`TP`)**: Defines a fixed profit target (e.g., $X) for each executed trade. When the TP payload is triggered, positions are automatically closed once the target gain is reached.
    - **Stop Loss (`SL`)**: Sets a fixed maximum loss limit (e.g., $X) for every trade. The SL payload ensures trades are closed automatically once the predefined loss threshold is hit, protecting against excessive drawdowns.
    - **Recalibrate (`RECALIBRATE`)**: Applies a default profit target of $500 with a trailing buffer of $70 to dynamically manage trade exits. This payload helps maintain overall portfolio profitability by triggering controlled closures during sudden market movements, ensuring near break-even results when volatility spikes.
    - **End of Day Close (`EOD_CLOSE`)**: Automatically manages open positions as the trading day nears its close. When active, this hook detects the daily market shutdown time and closes all open trades for the instrument approximately 2 minutes before the session ends, preventing overnight exposure or downtime-related risks.
    - **End of Week Close (`EOW_CLOSE`)**: Automatically closes all open positions for the instrument around 2 minutes before the market’s weekly close. This ensures no trades remain open through the weekend when markets are inactive, reducing gap risk on reopening.
    - **Strategy (`STRATEGY`) Switch**: Maintains trade direction discipline by ensuring only one active position per `epic` and `hook_name` (strategy identifier). If a new signal is received in the opposite direction (e.g., switching from BUY to SELL), the existing position is first closed before initiating the new trade.
    - **Hook Name (`HOOKNAME`) Switch**: Enables management of multiple independent strategies or webhook sources. Each unique hook name can represent a different trading logic or alert system, providing detailed control, tracking, and separation across strategies.

- **Comprehensive Trade History**: Gain detailed insights into your performance with a real-time view of all closed trades, including:
    - **Detailed PnL (Profit & Loss)**: Analyze the profitability of individual trades and overall strategies.
    - **Execution Timestamps**: Track when trades were opened and closed.
    - **Associated Strategy Data**: Link closed trades back to the specific strategies that initiated them.
    - **Persistent Storage with SQLite**: All trade history is stored in a local SQLite database, ensuring your trade records are retained across restarts and easily accessible for analysis.


---

## 🛠️ Tech Stack

- **Backend Framework**: **FastAPI** (Python) - A modern, fast (high-performance) web framework for building APIs with Python 3.7+ based on standard Python type hints.
- **HTTP Requests**: **`httpx`** - A powerful, user-friendly HTTP client for Python, supporting both synchronous and asynchronous requests, used for interacting with the Capital.com API.
- **Configuration Management**: **Environment variables** and potentially **`python-dotenv`** for loading configurations from a `.env` file, ensuring sensitive information is kept secure.
- **Web Server**: **`Uvicorn`** - An ASGI web server, recommended for running FastAPI applications due to its speed and asynchronous nature.

---

## 🚀 Installation & Environment Setup

This guide will walk you through setting up and running Capital Hook on your local machine.


### 1\. Clone the Repository

First, open your terminal or command prompt and clone the Capital Hook repository to your local machine:

```bash
git clone https://github.com/danieltonad/capital-hook.git
cd capital-hook
```

### 2\. Set Up a Virtual Environment (Recommended)

It's highly recommended to use a **virtual environment** for your project. This creates an isolated Python environment, preventing dependency conflicts with other Python projects on your system.

```bash
python -m venv venv
source venv/bin/activate  # On Linux/macOS
.\venv\Scripts\activate  # On Windows (use PowerShell or Git Bash for 'source' or just type the path)
```

You should see `(venv)` prepended to your terminal prompt, indicating that the virtual environment is active.

---

### 3\. Install Dependencies

With your virtual environment activated, install all the required Python packages listed in the `requirements.txt` file:

```bash
pip install -r requirements.txt
```

This command will download and install all necessary libraries for Capital Hook to run.

---

### 4\. Configure Capital.com API Credentials

Capital Hook needs your Capital.com API credentials to securely log in and execute trades on your behalf. For security best practices, these credentials should be stored as **environment variables** and never hardcoded directly into your application's source code.

Create a new file named `.env` in the root directory of your project (the same directory where `main.py` is located). Add the following lines, replacing the placeholder values with your actual Capital.com API credentials:

```ini
CAPITAL_IDENTITY="YOUR_CAPITAL_COM_IDENTITY"
CAPITAL_PASSWORD="YOUR_CAPITAL_COM_PASSWORD"
CAPITAL_API_KEY="YOUR_CAPITAL_COM_API_KEY"
```

---

## ⚙️ Usage Guide

### Running the Application

To start the Capital Hook FastAPI application, ensure your virtual environment is activated and then run the following command from the project's root directory:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

- `main:app`: This specifies that Uvicorn should run the FastAPI application instance named `app` found within `main.py`.
- `--host 0.0.0.0`: Makes the application accessible from all network interfaces on your machine. This is important if you plan to access the dashboard or send webhooks from another device or a deployed server.
- `--port 8000`: The port number on which the application will listen for incoming requests. You can change this if port 8000 is already in use.
- `--reload`: (Useful for development) This flag tells Uvicorn to automatically reload the application whenever it detects changes in your code, so you don't have to restart it manually.

Once the application is running, you should see output in your terminal indicating that Uvicorn is serving on the specified address and port.

### Accessing the Dashboard & Config Page

Once the Capital Hook application is running, you can access its web-based interfaces through your browser:

- **Dashboard**: Navigate to `http://localhost:8000/dashboard`
  - Here you can monitor your **real-time portfolio balance**, view **live positions**, and observe the status of your trades.
- **Config Page**: Navigate to `http://localhost:8000/config`
  - This page allows you to dynamically adjust various settings, including **webhook payload structures, Stop Loss/Take Profit defaults, and behavior switches** for market conditions or strategy identification.

### Configuring TradingView Webhooks

To automate your trades, you need to set up alerts in TradingView that send data to your Capital Hook instance via webhooks.

1.  **Determine your Capital Hook's Webhook URL**: This will be the public-facing URL of your deployed Capital Hook instance, followed by the webhook endpoint. Assuming your hook is running locally on port 8000, the default webhook URL would be `http://your-machine-ip:8000/webhook` (replace `your-machine-ip` with your computer's actual IP address if accessing from another device on your network, or your domain if deployed).

2.  **Create a TradingView Alert**:

    - Open TradingView and navigate to the chart or strategy from which you want to send alerts.
        - Click the "Alert" icon (the bell icon, typically on the right sidebar or from the top menu).
        - Configure your **Condition** (e.g., "Crossing," "Strategy Alert") and **Frequency** as needed for your strategy.
        - Under the "Notifications" tab, **check the "Webhook URL" box**.
        - In the "Webhook URL" field, enter the URL you determined in step 1 (e.g., `http://your-machine-ip:8000/webhook`).
        - In the **"Message"** field, construct a **JSON payload** that matches what Capital Hook expects. This payload should include the instrument, trade direction, amount, and any optional parameters like stop loss or take profit.

        **Example TradingView Alert Message (JSON Payload):**

        ```json
        {
        "epic": "{{ticker}}",
        "direction": "SELL",
        "amount": 100.0,
        "hook_name": "20/200EMA",
        "profit": 120.0,
        "loss": 50.0,
        "exit_criteria": ["TP", "SL", "STRATEGY", "RECALIBRATE", "EOD_CLOSE", "EOW_CLOSE"]
        }
        ```

        **Payload Field Reference:**
        - **`epic`**: TradingView placeholder for the instrument symbol (e.g., `EURUSD`, `AAPL`).
        - **`direction`**: Trade direction, either `"BUY"` or `"SELL"`.
        - **`amount`**: Trade size, specified in the currency or units set for your Capital.com account (e.g., USD, EUR, GBP, etc.).       
        - **`hook_name`**: Custom identifier for the strategy or alert.
        - **`profit`**: Take Profit value.
        - **`loss`**: Stop Loss value.
        - **`exit_criteria`**: Array specifying exit conditions, such as `["TP", "SL"]`.

        > **Note:** The keys and structure above reflect the default expected payload for Capital Hook. If you have customized your webhook handler, refer to your `main.py` or webhook handler code for the exact payload requirements. Always ensure your TradingView alert message is valid JSON.

---

## 🤝 Contributing

We welcome contributions! If you have ideas or fixes, please:

1. Fork the repo.
2. Create a branch for your change.
3. Make your edits and commit with clear messages.
4. Push to your fork.
5. Open a Pull Request to `main` with a short description.

Please follow Python best practices and add tests if needed.

---

## 📄 License

This project is licensed under the **MIT License**. You can find the full text of the license in the [LICENSE](https://github.com/danieltonad/capital-hook/blob/main/LICENSE) file in this repository. This open-source license allows you to use, modify, and distribute the software freely, subject to its terms.
