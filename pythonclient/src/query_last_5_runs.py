#!/usr/bin/env python3
"""
Query the last 5 runs from ChromaDB to get metadata for trading conditions.
Usage: python3 src/query_last_5_runs.py [symbol]
"""

import chromadb
from chromadb.utils import embedding_functions
import json
from datetime import datetime
import logging
import argparse
import sys

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize ChromaDB client and collection
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(
    name="backtest_results",
    embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
)

def extract_timestamp(result_id):
    """Extract timestamp from result ID."""
    try:
        # ID format: "symbol_2025-07-27 09:12:26.385527"
        timestamp_str = result_id.split("_", 1)[1]
        return datetime.fromisoformat(timestamp_str.replace(" ", "T"))
    except:
        return datetime.min

def get_last_5_runs(symbol: str = None):
    """Get the last 5 backtest runs with detailed metadata."""
    
    # Get all results
    results = collection.get(include=["documents", "metadatas"])
    
    if not results["metadatas"]:
        print("❌ No backtest results found in ChromaDB.")
        return None
    
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
        
        # Update results and all_ids with filtered data
        results = filtered_results
        all_ids = filtered_results["ids"]
    
    if not all_ids:
        print(f"❌ No backtest results found for symbol: {symbol}")
        return None
    
    # Sort by timestamp (most recent first)
    sorted_indices = sorted(range(len(all_ids)), 
                           key=lambda i: extract_timestamp(all_ids[i]), 
                           reverse=True)
    
    # Get the last 5 results
    last_5_indices = sorted_indices[:5]
    
    last_5_results = []
    for idx in last_5_indices:
        last_5_results.append({
            "id": all_ids[idx],
            "document": results["documents"][idx],
            "metadata": results["metadatas"][idx],
            "timestamp": extract_timestamp(all_ids[idx])
        })
    
    return last_5_results

def display_trading_conditions_summary(results):
    """Display a summary of trading conditions across all runs."""
    print("\n" + "="*80)
    print("📊 TRADING CONDITIONS SUMMARY (Last 5 Runs)")
    print("="*80)
    
    # Quick profit percentage overview
    print(f"\n📈 QUICK PERFORMANCE OVERVIEW:")
    profit_percentages = []
    for result in results:
        meta = result["metadata"]
        profit = meta.get('total_profit', 0)
        initial_capital = meta.get('initial_capital', 100000)
        if initial_capital > 0:
            profit_pct = (profit / initial_capital) * 100
            profit_percentages.append(profit_pct)
            symbol = meta.get('symbol', 'N/A')
            print(f"   {symbol}: {profit_pct:+.1f}% (${profit:,.0f})")
    
    if profit_percentages:
        avg_return = sum(profit_percentages) / len(profit_percentages)
        print(f"   📊 Average Return: {avg_return:+.1f}%")
        print(f"   📊 Best Return: {max(profit_percentages):+.1f}%")
        print(f"   📊 Worst Return: {min(profit_percentages):+.1f}%")
    
    # Collect all trading conditions
    conditions = {
        "adx_values": [],
        "volatility_values": [],
        "volume_changes": [],
        "sma_20_values": [],
        "sma_50_values": [],
        "consolidation_count": 0,
        "profit_values": [],
        "success_rates": [],
        "trade_counts": []
    }
    
    for result in results:
        meta = result["metadata"]
        
        # Market conditions
        if meta.get("adx") is not None:
            conditions["adx_values"].append(meta["adx"])
        if meta.get("volatility") is not None:
            conditions["volatility_values"].append(meta["volatility"])
        if meta.get("volume_change") is not None:
            conditions["volume_changes"].append(meta["volume_change"])
        if meta.get("sma_20") is not None:
            conditions["sma_20_values"].append(meta["sma_20"])
        if meta.get("sma_50") is not None:
            conditions["sma_50_values"].append(meta["sma_50"])
        if meta.get("valid_consolidation"):
            conditions["consolidation_count"] += 1
        
        # Performance metrics
        if meta.get("total_profit") is not None:
            conditions["profit_values"].append(meta["total_profit"])
        if meta.get("success_rate") is not None:
            conditions["success_rates"].append(meta["success_rate"])
        if meta.get("total_trades") is not None:
            conditions["trade_counts"].append(meta["total_trades"])
    
    # Calculate averages and ranges
    print(f"📈 MARKET CONDITIONS:")
    if conditions["adx_values"]:
        avg_adx = sum(conditions["adx_values"]) / len(conditions["adx_values"])
        min_adx = min(conditions["adx_values"])
        max_adx = max(conditions["adx_values"])
        print(f"   ADX: {avg_adx:.1f} (Range: {min_adx:.1f} - {max_adx:.1f})")
    
    if conditions["volatility_values"]:
        avg_vol = sum(conditions["volatility_values"]) / len(conditions["volatility_values"])
        min_vol = min(conditions["volatility_values"])
        max_vol = max(conditions["volatility_values"])
        print(f"   Volatility: {avg_vol:.3f} (Range: {min_vol:.3f} - {max_vol:.3f})")
    
    if conditions["volume_changes"]:
        avg_vol_change = sum(conditions["volume_changes"]) / len(conditions["volume_changes"])
        min_vol_change = min(conditions["volume_changes"])
        max_vol_change = max(conditions["volume_changes"])
        print(f"   Volume Change: {avg_vol_change:.2f} (Range: {min_vol_change:.2f} - {max_vol_change:.2f})")
    
    if conditions["sma_20_values"] and conditions["sma_50_values"]:
        avg_sma_20 = sum(conditions["sma_20_values"]) / len(conditions["sma_20_values"])
        avg_sma_50 = sum(conditions["sma_50_values"]) / len(conditions["sma_50_values"])
        print(f"   SMA 20: {avg_sma_20:.2f} | SMA 50: {avg_sma_50:.2f}")
    
    print(f"   Consolidation Periods: {conditions['consolidation_count']}/5 runs")
    
    print(f"\n💰 PERFORMANCE SUMMARY:")
    if conditions["profit_values"]:
        # Calculate profit percentages
        profit_percentages = []
        for i, profit in enumerate(conditions["profit_values"]):
            initial_capital = results[i]["metadata"].get("initial_capital", 100000)
            if initial_capital > 0:
                profit_pct = (profit / initial_capital) * 100
                profit_percentages.append(profit_pct)
        
        if profit_percentages:
            avg_profit_pct = sum(profit_percentages) / len(profit_percentages)
            min_profit_pct = min(profit_percentages)
            max_profit_pct = max(profit_percentages)
            print(f"   Average Return: {avg_profit_pct:+.1f}% (Range: {min_profit_pct:+.1f}% - {max_profit_pct:+.1f}%)")
        
        # Also show profit values for reference
        avg_profit = sum(conditions["profit_values"]) / len(conditions["profit_values"])
        min_profit = min(conditions["profit_values"])
        max_profit = max(conditions["profit_values"])
        print(f"   Average Profit: ${avg_profit:,.2f} (Range: ${min_profit:,.2f} - ${max_profit:,.2f})")
    
    if conditions["success_rates"]:
        avg_success = sum(conditions["success_rates"]) / len(conditions["success_rates"])
        min_success = min(conditions["success_rates"])
        max_success = max(conditions["success_rates"])
        print(f"   Average Success Rate: {avg_success:.1f}% (Range: {min_success:.1f}% - {max_success:.1f}%)")
    
    if conditions["trade_counts"]:
        avg_trades = sum(conditions["trade_counts"]) / len(conditions["trade_counts"])
        min_trades = min(conditions["trade_counts"])
        max_trades = max(conditions["trade_counts"])
        print(f"   Average Trades: {avg_trades:.0f} (Range: {min_trades} - {max_trades})")

def display_detailed_run(run_data, run_number):
    """Display detailed information for a single run."""
    print(f"\n{'='*20} RUN #{run_number} {'='*20}")
    print(f"📅 Timestamp: {run_data['timestamp']}")
    print(f"🆔 ID: {run_data['id']}")
    print(f"📄 Summary: {run_data['document']}")
    
    meta = run_data["metadata"]
    
    # Market Conditions
    print(f"\n📊 MARKET CONDITIONS:")
    print(f"   Symbol: {meta.get('symbol', 'N/A')}")
    print(f"   ADX: {meta.get('adx', 'N/A')}")
    print(f"   Volatility: {meta.get('volatility', 'N/A')}")
    print(f"   Volume Change: {meta.get('volume_change', 'N/A')}")
    print(f"   SMA 20: {meta.get('sma_20', 'N/A')}")
    print(f"   SMA 50: {meta.get('sma_50', 'N/A')}")
    print(f"   Consolidation: {meta.get('valid_consolidation', 'N/A')}")
    
    # Performance Metrics
    print(f"\n💰 PERFORMANCE METRICS:")
    initial_capital = meta.get('initial_capital', 0)
    final_capital = meta.get('final_capital', 0)
    total_profit = meta.get('total_profit', 0)
    
    print(f"   Initial Capital: ${initial_capital:,.2f}")
    print(f"   Final Capital: ${final_capital:,.2f}")
    print(f"   Total Profit: ${total_profit:,.2f}")
    
    # Calculate and display return percentage prominently
    if initial_capital > 0:
        return_pct = (total_profit / initial_capital) * 100
        print(f"   📈 Return: {return_pct:+.1f}%")
    else:
        print(f"   📈 Return: N/A")
    
    print(f"   Success Rate: {meta.get('success_rate', 0):.1f}%")
    print(f"   Total Trades: {meta.get('total_trades', 0)}")
    
    # Strategy Parameters
    print(f"\n⚙️  STRATEGY PARAMETERS:")
    print(f"   Risk per Trade: ${meta.get('risk_per_trade', 0):,.2f}")
    print(f"   Stop Loss %: {meta.get('stop_loss_pct', 0):.2f}%")
    print(f"   Target %: {meta.get('target_pct', 0):.2f}%")
    print(f"   Bar Size: {meta.get('bar_size', 'N/A')}")
    print(f"   Duration: {meta.get('duration', 'N/A')}")
    print(f"   Paper Trading: {meta.get('paper_trading', 'N/A')}")
    
    # Trading Conditions Analysis
    print(f"\n🔍 TRADING CONDITIONS ANALYSIS:")
    
    # ADX Analysis
    adx = meta.get('adx')
    if adx is not None:
        if adx > 25:
            trend_strength = "Strong"
        elif adx > 20:
            trend_strength = "Moderate"
        else:
            trend_strength = "Weak"
        print(f"   Trend Strength: {trend_strength} (ADX: {adx:.1f})")
    
    # Volatility Analysis
    volatility = meta.get('volatility')
    if volatility is not None:
        if volatility > 0.05:
            vol_level = "High"
        elif volatility > 0.02:
            vol_level = "Medium"
        else:
            vol_level = "Low"
        print(f"   Volatility Level: {vol_level} ({volatility:.3f})")
    
    # Volume Analysis
    volume_change = meta.get('volume_change')
    if volume_change is not None:
        if volume_change > 0.5:
            volume_level = "High"
        elif volume_change > 0.2:
            volume_level = "Moderate"
        else:
            volume_level = "Low"
        print(f"   Volume Activity: {volume_level} ({volume_change:.2f})")
    
    # SMA Trend Analysis
    sma_20 = meta.get('sma_20')
    sma_50 = meta.get('sma_50')
    if sma_20 is not None and sma_50 is not None:
        if sma_20 > sma_50:
            trend = "Bullish"
        else:
            trend = "Bearish"
        print(f"   SMA Trend: {trend} (20: {sma_20:.2f}, 50: {sma_50:.2f})")
    
    # Performance Rating
    success_rate = meta.get('success_rate', 0)
    if success_rate > 60:
        rating = "Excellent"
    elif success_rate > 40:
        rating = "Good"
    elif success_rate > 20:
        rating = "Profitable"
    else:
        rating = "Loss"
    print(f"   Performance Rating: {rating}")

def analyze_patterns(results):
    """Analyze patterns across the last 5 runs."""
    print(f"\n{'='*20} PATTERN ANALYSIS {'='*20}")
    
    # Find best and worst performing runs
    profits = [(i, r["metadata"].get("total_profit", 0)) for i, r in enumerate(results)]
    profits.sort(key=lambda x: x[1], reverse=True)
    
    best_run = profits[0]
    worst_run = profits[-1]
    
    print(f"🏆 BEST PERFORMING RUN: #{best_run[0] + 1}")
    best_meta = results[best_run[0]]["metadata"]
    best_profit = best_meta.get('total_profit', 0)
    best_initial = best_meta.get('initial_capital', 100000)
    best_return_pct = (best_profit / best_initial * 100) if best_initial > 0 else 0
    print(f"   Return: {best_return_pct:+.1f}% (${best_profit:,.2f})")
    print(f"   Success Rate: {best_meta.get('success_rate', 0):.1f}%")
    print(f"   ADX: {best_meta.get('adx', 'N/A')}")
    print(f"   Volatility: {best_meta.get('volatility', 'N/A')}")
    
    print(f"\n📉 WORST PERFORMING RUN: #{worst_run[0] + 1}")
    worst_meta = results[worst_run[0]]["metadata"]
    worst_profit = worst_meta.get('total_profit', 0)
    worst_initial = worst_meta.get('initial_capital', 100000)
    worst_return_pct = (worst_profit / worst_initial * 100) if worst_initial > 0 else 0
    print(f"   Return: {worst_return_pct:+.1f}% (${worst_profit:,.2f})")
    print(f"   Success Rate: {worst_meta.get('success_rate', 0):.1f}%")
    print(f"   ADX: {worst_meta.get('adx', 'N/A')}")
    print(f"   Volatility: {worst_meta.get('volatility', 'N/A')}")
    
    # Identify optimal conditions
    print(f"\n🎯 OPTIMAL TRADING CONDITIONS:")
    
    # ADX range for best performance
    best_adx = best_meta.get('adx')
    worst_adx = worst_meta.get('adx')
    if best_adx is not None and worst_adx is not None:
        if best_adx > worst_adx:
            print(f"   Higher ADX ({best_adx:.1f}) correlates with better performance")
        else:
            print(f"   Lower ADX ({best_adx:.1f}) correlates with better performance")
    
    # Volatility range for best performance
    best_vol = best_meta.get('volatility')
    worst_vol = worst_meta.get('volatility')
    if best_vol is not None and worst_vol is not None:
        if best_vol > worst_vol:
            print(f"   Higher volatility ({best_vol:.3f}) correlates with better performance")
        else:
            print(f"   Lower volatility ({best_vol:.3f}) correlates with better performance")

def main():
    """Main function to query and display the last 5 runs."""
    parser = argparse.ArgumentParser(description="Query last 5 backtest runs from ChromaDB")
    parser.add_argument("symbol", nargs="?", help="Filter by specific symbol (e.g., PLTR)")
    parser.add_argument("--summary-only", action="store_true", help="Show only summary, not detailed runs")
    args = parser.parse_args()
    
    print("🔍 QUERYING LAST 5 BACKTEST RUNS")
    print("=" * 60)
    
    if args.symbol:
        print(f"📊 Filtering for symbol: {args.symbol.upper()}")
    
    # Get last 5 runs
    results = get_last_5_runs(symbol=args.symbol)
    
    if not results:
        return
    
    print(f"✅ Found {len(results)} runs")
    
    # Display summary
    display_trading_conditions_summary(results)
    
    # Display detailed runs if not summary-only
    if not args.summary_only:
        for i, result in enumerate(results, 1):
            display_detailed_run(result, i)
    
    # Analyze patterns
    analyze_patterns(results)
    
    print("\n" + "=" * 60)
    print("✅ Analysis complete!")

if __name__ == "__main__":
    main() 