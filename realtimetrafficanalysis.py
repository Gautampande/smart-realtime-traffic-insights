import os
import pandas as pd
import json
import time
import requests
import xgboost as xgb
import psycopg2
import streamlit as st
from sqlalchemy import create_engine
from kafka import KafkaProducer, KafkaConsumer
from kafka.errors import KafkaError
from pmdarima import auto_arima
from dotenv import load_dotenv
import io
import threading
from sklearn.metrics import accuracy_score
import altair as alt


# Load environment variables from .env
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
KAFKA_BROKER_URL = os.getenv("KAFKA_BROKER_URL")
CA_CERT_PATH = os.getenv("CA_CERT_PATH")
SSL_CERT_PATH = os.getenv("SSL_CERT_PATH")
SSL_KEY_PATH = os.getenv("SSL_KEY_PATH")

ssl_config = {
    'security_protocol': 'SSL',
    'ssl_cafile': CA_CERT_PATH,
    'ssl_certfile': SSL_CERT_PATH,
    'ssl_keyfile': SSL_KEY_PATH,
    'ssl_check_hostname': True
}
engine = create_engine(DATABASE_URL)

# 📥 Ensure PostgreSQL table exists
def ensure_realtime_table_exists():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS realtime_traffic_data (
            segmentid TEXT,
            street TEXT,
            direction TEXT,
            from_street TEXT,
            to_street TEXT,
            length FLOAT,
            street_heading TEXT,
            comments TEXT,
            start_longitude FLOAT,
            start_latitude FLOAT,
            end_longitude FLOAT,
            end_latitude FLOAT,
            current_speed FLOAT,
            last_updated TEXT
        )
    """)
    conn.commit()
    conn.close()
    print("✅ realtime_traffic_data table ensured")

# 🧊 Load batch data
def load_csv_to_postgres():
    files = {
        "historical_traffic": "data/historical_traffic.csv",
        "red_light_violations": "data/red_light_violations.csv",
        "speed_camera_violations": "data/speed_camera_violations.csv",
        "traffic_crashes": "data/traffic_crashes.csv"
    }
    for table, path in files.items():
        df = pd.read_csv(path)
        df.to_sql(table, engine, if_exists="replace", index=False)
    print("✅ Batch data loaded")

# 🚚 Produce real-time data to Kafka
def produce_realtime_data():
    api_url = "https://data.cityofchicago.org/resource/n4j6-wkkf.csv"
    try:
        response = requests.get(api_url)
        df = pd.read_csv(io.StringIO(response.text))

        producer = KafkaProducer(
            bootstrap_servers=KAFKA_BROKER_URL,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            **ssl_config
        )
        for _, row in df.iterrows():
            producer.send('realtime_traffic', row.to_dict())
        print("✅ Real-time data sent to Kafka")
    except Exception as e:
        print(f"❌ Failed to fetch/send data: {e}")

# 📦 Kafka Consumer → PostgreSQL
def consume_realtime_data():
    try:
        ensure_realtime_table_exists()
        consumer = KafkaConsumer(
            'realtime_traffic',
            bootstrap_servers=KAFKA_BROKER_URL,
            auto_offset_reset='earliest',
            value_deserializer=lambda m: json.loads(m.decode('utf-8')),
            consumer_timeout_ms=10000,
            **ssl_config
        )

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        record_count = 0

        for msg in consumer:
            d = {k.lower(): v for k, v in msg.value.items()}
            fields = [
                "segmentid", "street", "direction", "from_street", "to_street",
                "length", "street_heading", "comments", "start_longitude",
                "start_latitude", "end_longitude", "end_latitude",
                "current_speed", "last_updated"
            ]
            values = tuple(d.get(k, None) for k in fields)
            cur.execute(f"""
                INSERT INTO realtime_traffic_data ({', '.join(fields)})
                VALUES ({', '.join(['%s'] * len(fields))})
            """, values)
            conn.commit()

            record_count += 1
            if record_count >= 200:
                print("✅ 200 real-time records processed")
                break

    except Exception as e:
        print(f"❌ Kafka Error: {e}")

# 🚦 XGBoost Model Training
def train_xgboost():
    df = pd.read_sql("SELECT * FROM historical_traffic", engine)
    df['is_congested'] = df['speed'] < 20
    X = df[['bus_count', 'length']].fillna(0)
    y = df['is_congested']
    model = xgb.XGBClassifier()
    model.fit(X, y)
    accuracy = accuracy_score(y, model.predict(X))
    print("✅ XGBoost model trained with accuracy:", accuracy)
    return model, X.columns.tolist(), accuracy


# 🔮 ARIMA Forecast
def run_arima(street):
    df = pd.read_sql("SELECT * FROM historical_traffic", engine)
    df = df[df['street'] == street].copy()
    if len(df) < 10 or df['speed'].isnull().all():
        return None
    df['time'] = pd.to_datetime(df['time'])
    df.sort_values('time', inplace=True)
    model = auto_arima(df['speed'].ffill(), seasonal=False)
    return model.predict(n_periods=5)

# 🖥️ Streamlit UI
def run_dashboard():
    st.set_page_config(layout="wide")
    st.title("🚦 Traffic Congestion Dashboard")

    df = pd.read_sql("SELECT * FROM realtime_traffic_data", engine)
    df['current_speed'] = pd.to_numeric(df['current_speed'], errors='coerce')
    df['current_speed'] = df['current_speed'].replace(-1, pd.NA)
    df.dropna(subset=['current_speed'], inplace=True)
    df['is_congested'] = df['current_speed'] < 20

    # Train and apply XGBoost
    model, feature_order, accuracy = train_xgboost()
    df_xgb = df[['length']].copy()
    df_xgb['bus_count'] = 2  # Default/mock value
    df_xgb = df_xgb[feature_order]  # Ensure order match
    df['predicted_congestion'] = model.predict(df_xgb)

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "Heatmap", "Summary", "Street Filter", "Forecast", "Model Predictions"
    ])

    with tab1:
        st.subheader("🧊 Congestion Heatmap")
        styled = df[['segmentid', 'street', 'current_speed']].style.background_gradient(
            subset=['current_speed'], cmap='RdYlGn_r')
        st.dataframe(styled)

    with tab2:
        st.subheader("📊 Speed Summary")
        chart_data = df[['segmentid', 'current_speed']].dropna()

        bar_chart = alt.Chart(chart_data).mark_bar().encode(
            x=alt.X('segmentid:N', title='Segment ID'),
            y=alt.Y('current_speed:Q', title='Current Speed (mph)')
        )
        st.altair_chart(bar_chart, use_container_width=True)

        st.subheader("🚥 Congestion Counts")
        summary = df['is_congested'].value_counts().rename({0: "Clear", 1: "Congested"})
        summary_df = summary.reset_index()
        summary_df.columns = ['Condition', 'Count']

        summary_chart = alt.Chart(summary_df).mark_bar().encode(
            x=alt.X('Condition:N', title='Traffic Condition'),
            y=alt.Y('Count:Q', title='Count')
        )
        st.altair_chart(summary_chart, use_container_width=True)

    with tab3:
        st.subheader("🔍 Filter by Street")
        streets = df['street'].dropna().unique().tolist()
        selected = st.selectbox("Street", streets)

        @st.cache_data
        def filter_street(s):
            return df[df['street'] == s]

        st.dataframe(filter_street(selected))

    with tab4:
        st.subheader("📈 Speed Forecast (ARIMA)")
        hist_df = pd.read_sql("SELECT street FROM historical_traffic GROUP BY street HAVING COUNT(*) >= 10", engine)
        streets = hist_df['street'].tolist()
        if streets:
            selected_street = st.selectbox("Choose Street", streets)
            forecast = run_arima(selected_street)
            if forecast is not None:
                forecast_df = pd.DataFrame({'Time': list(range(len(forecast))), 'Speed': forecast})
                forecast_chart = alt.Chart(forecast_df).mark_line().encode(
                    x=alt.X('Time:Q', title='Forecast Step'),
                    y=alt.Y('Speed:Q', title='Predicted Speed (mph)')
                )
                st.altair_chart(forecast_chart, use_container_width=True)
            else:
                st.warning("⚠️ Not enough data for forecast.")

    with tab5:
        st.subheader("🧠 Congestion Prediction (XGBoost)")
        st.write("Based on road `length` and `bus_count`")
        st.write(f"**Model Accuracy:** {accuracy:.2%}")
        st.dataframe(df[['segmentid', 'street', 'length', 'predicted_congestion']])

# 🚀 Main
def main():
    ensure_realtime_table_exists()
    load_csv_to_postgres()
    threading.Thread(target=consume_realtime_data).start()
    run_dashboard()

if __name__ == "__main__":
    main()
