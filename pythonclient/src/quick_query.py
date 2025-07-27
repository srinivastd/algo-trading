#!/usr/bin/env python3
"""
Quick ChromaDB query script for most recent backtest results.
Usage: python src/quick_query.py [symbol]
"""

import chromadb
from chromadb.utils import embedding_functions
from datetime import datetime
import sys

def quick_query(symbol=None):
    """Quick query for most recent backtest results."""
    
    # Initialize ChromaDB
    chroma_client = chromadb.PersistentClient(path="./chroma_db")
    collection = chroma_client.get_or_create_collection(
        name="backtest_results",
        embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    )
    
    # Get all results
    results = collection.get(include=["documents", "metadatas"])
    
    if not results["metadatas"]:
        print("❌ No backtest results found in ChromaDB.")
        return
    
    # Get IDs separately
    all_ids = collection.get()["ids"]
    
    # Filter by symbol if specified
    if symbol:
        filtered_results = {
            "ids": [],
            "documents": [],
            "metadatas": []
        }
        for i, meta in enumerate(results["metadatas"]):
            if meta.get("symbol") == symbol.upper():
                filtered_results["ids"].append(all_ids[i])
                filtered_results["documents"].append(results["documents"][i])
                filtered_results["metadatas"].append(results["metadatas"][i])
        results = filtered_results
        all_ids = filtered_results["ids"]
    
    if not all_ids:
        print(f"❌ No results found for symbol: {symbol}")
        return
    
    # Sort by timestamp (most recent first)
    def extract_timestamp(result_id):
        try:
            timestamp_str = result_id.split("_", 1)[1]
            return datetime.fromisoformat(timestamp_str.replace(" ", "T"))
        except:
            return datetime.min
    
    sorted_indices = sorted(range(len(all_ids)), 
                           key=lambda i: extract_timestamp(all_ids[i]), 
                           reverse=True)
    
    # Get most recent
    most_recent_idx = sorted_indices[0]
    meta = results["metadatas"][most_recent_idx]
    doc = results["documents"][most_recent_idx]
    timestamp = extract_timestamp(all_ids[most_recent_idx])
    
    # Display summary
    print(f"📅 {timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📊 {meta.get('symbol', 'N/A')} - {doc}")
    print(f"💰 Profit: ${meta.get('total_profit', 0):,.2f}")
    print(f"📈 Return: {((meta.get('final_capital', 0) - meta.get('initial_capital', 0)) / meta.get('initial_capital', 1) * 100):+.1f}%")
    print(f"🎯 Success: {meta.get('success_rate', 0):.1f}% ({meta.get('total_trades', 0)} trades)")
    print(f"📊 ADX: {meta.get('adx', 'N/A'):.1f} | Vol: {meta.get('volatility', 'N/A'):.4f}")
    print(f"⚙️  Risk: ${meta.get('risk_per_trade', 0):,.0f} | SL: {meta.get('stop_loss_pct', 0):.1f}% | Target: {meta.get('target_pct', 0):.1f}%")

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else None
    quick_query(symbol) 