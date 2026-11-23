"""Load 2,000 Yelp reviews from Hugging Face into Postgres as the raw_reviews table.

If Yelp can't be downloaded, the loader falls back to other public review datasets, then to
synthetic data, so the dbt models always have input. DB_* settings come from the environment.
"""
import os

import numpy as np
import pandas as pd
from datasets import load_dataset
from sqlalchemy import create_engine
import warnings
warnings.filterwarnings('ignore')

# Connection settings come from environment variables; nothing secret is stored here.
DB_USER = os.getenv('DB_USER', 'dbt_user')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'dbt_analytics')

connection_string = f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'

print("Connecting to PostgreSQL...")
engine = create_engine(connection_string)

def load_ecommerce_data():
    """Load 2,000 reviews from Hugging Face into Postgres as raw_reviews."""
    print("=" * 60)
    print("Loading dataset from HuggingFace...")
    print("=" * 60)

    datasets_to_try = [
        {
            "name": "Yelp Reviews",
            "dataset": "yelp_review_full",
            "subset": None,
            "split": "train[:2000]",
            "params": {}
        },
        {
            "name": "Amazon Product Reviews (Parquet format)",
            "dataset": "amazon_us_reviews",
            "subset": "Video_Games_v1_00",
            "split": "train[:2000]",
            "params": {}
        },
        {
            "name": "IMDB Movie Reviews",
            "dataset": "imdb",
            "subset": None,
            "split": "train[:2000]",
            "params": {}
        }
    ]

    df = None
    dataset_used = None

    for dataset_info in datasets_to_try:
        try:
            print(f"\nTrying: {dataset_info['name']}...")

            if dataset_info['subset']:
                dataset = load_dataset(
                    dataset_info['dataset'],
                    dataset_info['subset'],
                    split=dataset_info['split'],
                    **dataset_info['params']
                )
            else:
                dataset = load_dataset(
                    dataset_info['dataset'],
                    split=dataset_info['split'],
                    **dataset_info['params']
                )

            df = dataset.to_pandas()
            dataset_used = dataset_info['name']
            print(f"✓ Successfully loaded: {dataset_info['name']}")
            break

        except Exception as e:
            print(f"✗ Failed to load {dataset_info['name']}: {str(e)[:100]}")
            continue

    if df is None:
        print("\n" + "=" * 60)
        print("All datasets failed. Creating synthetic data for demo...")
        print("=" * 60)
        df = create_synthetic_data()
        dataset_used = "Synthetic Demo Data"

    df = standardize_columns(df, dataset_used)

    print("\n" + "=" * 60)
    print(f"Dataset Used: {dataset_used}")
    print(f"Total Records: {len(df)}")
    print("=" * 60)
    print("\nSample of data:")
    print(df.head(3))
    print("\n" + "=" * 60)
    print(f"Columns: {list(df.columns)}")
    print("=" * 60)

    print("\nLoading data to PostgreSQL...")
    df.to_sql(
        'raw_reviews', 
        engine, 
        if_exists='replace',
        index=False, 
        schema='public'
    )

    print("=" * 60)
    print(f"✓ SUCCESS! Loaded {len(df)} records to 'raw_reviews' table")
    print("=" * 60)

    print("\nVerifying data in PostgreSQL...")
    verify_df = pd.read_sql("SELECT * FROM raw_reviews LIMIT 5", engine)
    print(verify_df)

    return df

def standardize_columns(df, dataset_name):
    """Map each dataset's columns onto the schema the dbt staging model expects."""

    if "Amazon" in dataset_name or "product_id" in df.columns:
        column_mapping = {
            'star_rating': 'rating',
            'review_headline': 'title',
            'review_body': 'text',
            'product_title': 'product_name',
            'verified_purchase': 'verified_purchase',
            'helpful_votes': 'helpful_vote'
        }
        df = df.rename(columns=column_mapping)

        if 'product_id' not in df.columns:
            df['product_id'] = 'PROD_' + df.index.astype(str)
        if 'user_id' not in df.columns:
            df['user_id'] = 'USER_' + df.index.astype(str)
        if 'timestamp' not in df.columns:
            df['timestamp'] = pd.Timestamp.now().timestamp()
        if 'verified_purchase' not in df.columns:
            df['verified_purchase'] = True
        if 'helpful_vote' not in df.columns:
            df['helpful_vote'] = 0

    elif "Yelp" in dataset_name:
        # yelp_review_full stores stars as label 0-4 and has no business, user or date
        # fields. Those are simulated (fixed seed) from small pools, so a business gets
        # many reviews and a user writes several: what the product and user models need.
        rng = np.random.default_rng(42)
        n = len(df)
        df['rating'] = df['label'] + 1
        df['title'] = 'Yelp Review'
        df['product_id'] = [f'BUSINESS_{i:03d}' for i in rng.integers(1, 101, n)]
        df['user_id'] = [f'USER_{i:04d}' for i in rng.integers(1, 601, n)]
        df['timestamp'] = pd.Timestamp.now().timestamp() - rng.integers(0, 365 * 24 * 3600, n)
        df['verified_purchase'] = rng.random(n) < 0.7
        df['helpful_vote'] = rng.poisson(2, n)

    elif "IMDB" in dataset_name or "imdb" in dataset_name:
        df = df.rename(columns={'text': 'text', 'label': 'sentiment_label'})
        df['rating'] = df['sentiment_label'].apply(lambda x: 5 if x == 1 else 1)
        df['title'] = 'Movie Review'
        df['product_id'] = 'MOVIE_' + df.index.astype(str)
        df['user_id'] = 'USER_' + df.index.astype(str)
        df['timestamp'] = pd.Timestamp.now().timestamp()
        df['verified_purchase'] = True
        df['helpful_vote'] = 0

    required_columns = ['rating', 'title', 'text', 'product_id', 'user_id', 
                       'timestamp', 'verified_purchase', 'helpful_vote']

    for col in required_columns:
        if col not in df.columns:
            if col == 'rating':
                df[col] = 5
            elif col == 'title':
                df[col] = 'Review'
            elif col == 'text':
                df[col] = 'Sample review text'
            elif col in ['product_id', 'user_id']:
                df[col] = col.upper() + '_' + df.index.astype(str)
            elif col == 'timestamp':
                df[col] = pd.Timestamp.now().timestamp()
            elif col == 'verified_purchase':
                df[col] = True
            elif col == 'helpful_vote':
                df[col] = 0

    df = df[required_columns]

    return df

def create_synthetic_data():
    """Create synthetic review data for demo purposes"""
    import random

    products = ['PROD_001', 'PROD_002', 'PROD_003', 'PROD_004', 'PROD_005']
    users = [f'USER_{i:03d}' for i in range(1, 51)]

    reviews = []
    review_texts = [
        "Great product! Highly recommend it.",
        "Not bad, but could be better.",
        "Terrible quality. Do not buy.",
        "Excellent value for money!",
        "Average product, nothing special.",
        "Love it! Will buy again.",
        "Disappointed with this purchase.",
        "Perfect! Exactly what I needed.",
        "Poor quality, broke after one use.",
        "Amazing! Exceeded my expectations."
    ]

    titles = [
        "Love it!", "Not impressed", "Great buy", "Waste of money",
        "Highly recommend", "Could be better", "Perfect!", "Disappointed",
        "Excellent", "Poor quality"
    ]

    for i in range(2000):
        review = {
            'rating': random.randint(1, 5),
            'title': random.choice(titles),
            'text': random.choice(review_texts),
            'product_id': random.choice(products),
            'user_id': random.choice(users),
            'timestamp': pd.Timestamp.now().timestamp() - random.randint(0, 365*24*60*60),
            'verified_purchase': random.choice([True, False]),
            'helpful_vote': random.randint(0, 50)
        }
        reviews.append(review)

    return pd.DataFrame(reviews)

if __name__ == "__main__":
    try:
        load_ecommerce_data()
        print("\n" + "=" * 60)
        print("Data ingestion complete.")
        print("=" * 60)
        print("Next: cd dbt_project/analytics_project && dbt run && dbt test")
        print("=" * 60)
    except Exception as e:
        print(f"\nError: {e}")
        print("\nPlease check:")
        print("1. PostgreSQL is running")
        print("2. Database 'dbt_analytics' exists")
        print("3. DB_USER / DB_PASSWORD are set correctly")
        print("4. You can reach huggingface.co")
        import traceback
        traceback.print_exc()
