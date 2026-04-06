import chromadb
from chromadb.utils import embedding_functions
import json
from datetime import datetime
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize ChromaDB client and collection
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(
    name="backtest_results",
    embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
)

def get_most_recent_backtest(symbol: str = None):
    """Get the most recent backtest result with detailed market conditions and performance metrics."""
    
    # Get all results
    results = collection.get(include=["documents", "metadatas"])
    
    if not results["metadatas"]:
        print("No backtest results found in ChromaDB.")
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
            if meta.get("symbol") == symbol:
                filtered_results["ids"].append(all_ids[i])
                filtered_results["documents"].append(results["documents"][i])
                filtered_results["metadatas"].append(results["metadatas"][i])
        
        # Update results and all_ids with filtered data
        results = filtered_results
        all_ids = filtered_results["ids"]
    
    if not all_ids:
        print(f"No backtest results found for symbol: {symbol}")
        return None
    
    # Sort by timestamp (assuming ID contains timestamp)
    # Extract timestamp from ID and sort
    def extract_timestamp(result_id):
        try:
            # ID format: "symbol_2025-07-27 09:12:26.385527"
            timestamp_str = result_id.split("_", 1)[1]
            return datetime.fromisoformat(timestamp_str.replace(" ", "T"))
        except:
            return datetime.min
    
    # Sort by timestamp (most recent first)
    sorted_indices = sorted(range(len(all_ids)), 
                           key=lambda i: extract_timestamp(all_ids[i]), 
                           reverse=True)
    
    # Get the most recent result
    most_recent_idx = sorted_indices[0]
    most_recent_id = all_ids[most_recent_idx]
    most_recent_doc = results["documents"][most_recent_idx]
    most_recent_meta = results["metadatas"][most_recent_idx]
    
    return {
        "id": most_recent_id,
        "document": most_recent_doc,
        "metadata": most_recent_meta,
        "timestamp": extract_timestamp(most_recent_id)
    }

def display_market_conditions(metadata):
    """Display market conditions from the backtest metadata."""
    print("📊 MARKET CONDITIONS")
    print("=" * 50)
    
    # Core market indicators
    indicators = {
        "ADX": metadata.get("adx"),
        "Volatility": metadata.get("volatility"),
        "SMA 20": metadata.get("sma_20"),
        "SMA 50": metadata.get("sma_50"),
        "Volume Change": metadata.get("volume_change"),
        "Valid Consolidation": metadata.get("valid_consolidation")
    }
    
    for indicator, value in indicators.items():
        if value is not None:
            if isinstance(value, bool):
                status = "✅" if value else "❌"
                print(f"{indicator:15}: {status}")
            elif isinstance(value, float):
                print(f"{indicator:15}: {value:.4f}")
            else:
                print(f"{indicator:15}: {value}")
    
    # Trend analysis
    if metadata.get("sma_20") and metadata.get("sma_50"):
        trend = "📈 BULLISH" if metadata["sma_20"] > metadata["sma_50"] else "📉 BEARISH"
        print(f"Trend Analysis: {trend}")
    
    # Market strength
    if metadata.get("adx"):
        adx = metadata["adx"]
        if adx > 25:
            strength = "🔥 STRONG TREND"
        elif adx > 20:
            strength = "⚡ MODERATE TREND"
        else:
            strength = "🌊 WEAK TREND"
        print(f"Market Strength: {strength}")

def display_performance_metrics(metadata):
    """Display performance metrics from the backtest metadata."""
    print("\n💰 PERFORMANCE METRICS")
    print("=" * 50)
    
    # Core performance metrics
    initial_capital = metadata.get("initial_capital", 0)
    final_capital = metadata.get("final_capital", 0)
    total_profit = metadata.get("total_profit", 0)
    success_rate = metadata.get("success_rate", 0)
    total_trades = metadata.get("total_trades", 0)
    
    # Calculate return percentage
    if initial_capital > 0:
        return_pct = ((final_capital - initial_capital) / initial_capital) * 100
    else:
        return_pct = 0
    
    print(f"Initial Capital: ${initial_capital:,.2f}")
    print(f"Final Capital:   ${final_capital:,.2f}")
    print(f"Total Profit:    ${total_profit:,.2f}")
    print(f"Return %:        {return_pct:+.2f}%")
    print(f"Success Rate:    {success_rate:.1f}%")
    print(f"Total Trades:    {total_trades}")
    
    # Performance rating
    if return_pct > 50:
        rating = "🏆 EXCELLENT"
    elif return_pct > 20:
        rating = "🎯 GOOD"
    elif return_pct > 0:
        rating = "✅ PROFITABLE"
    else:
        rating = "📉 LOSS"
    print(f"Performance:     {rating}")

def display_strategy_parameters(metadata):
    """Display strategy parameters used in the backtest."""
    print("\n⚙️  STRATEGY PARAMETERS")
    print("=" * 50)
    
    params = {
        "Strategy": metadata.get("strategy", "legacy (no tag)"),
        "Risk per Trade": f"${metadata.get('risk_per_trade', 0):,.2f}",
        "Stop Loss %": f"{metadata.get('stop_loss_pct', 0):.1f}%",
        "Target %": f"{metadata.get('target_pct', 0):.1f}%",
        "Bar Size": metadata.get("bar_size", "N/A"),
        "Duration": metadata.get("duration", "N/A"),
        "Paper Trading": "Yes" if metadata.get("paper_trading") else "No"
    }
    
    for param, value in params.items():
        print(f"{param:15}: {value}")

def display_trade_details(metadata):
    """Display detailed trade information if available."""
    print("\n📋 TRADE DETAILS")
    print("=" * 50)
    
    trades_str = metadata.get("trades", "[]")
    try:
        trades = eval(trades_str)  # Convert string representation back to list
        if trades:
            print(f"Number of trades: {len(trades)}")
            
            # Show first few trades
            for i, trade in enumerate(trades[:3]):  # Show first 3 trades
                print(f"\nTrade {i+1}:")
                print(f"  Type: {trade.get('type', 'N/A')}")
                print(f"  Entry: ${trade.get('entry_price', 0):.2f}")
                print(f"  Exit: ${trade.get('exit_price', 0):.2f}")
                print(f"  Profit: ${trade.get('profit', 0):.2f}")
                print(f"  Shares: {trade.get('shares', 0)}")
            
            if len(trades) > 3:
                print(f"\n... and {len(trades) - 3} more trades")
        else:
            print("No trades executed")
    except:
        print("Trade details not available in readable format")

def main():
    """Main function to query and display the most recent backtest."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Query most recent backtest results from ChromaDB")
    parser.add_argument("--symbol", type=str, help="Filter by specific symbol (e.g., NVDA)")
    args = parser.parse_args()
    
    print("🔍 QUERYING MOST RECENT BACKTEST")
    print("=" * 60)
    
    # Get most recent backtest
    result = get_most_recent_backtest(symbol=args.symbol)
    
    if not result:
        return
    
    # Display timestamp
    print(f"📅 Timestamp: {result['timestamp']}")
    print(f"🆔 ID: {result['id']}")
    print(f"📄 Summary: {result['document']}")
    
    # Display detailed information
    display_market_conditions(result['metadata'])
    display_performance_metrics(result['metadata'])
    display_strategy_parameters(result['metadata'])
    display_trade_details(result['metadata'])
    
    print("\n" + "=" * 60)
    print("✅ Analysis complete!")

if __name__ == "__main__":
    main() 