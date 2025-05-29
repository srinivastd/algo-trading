import chromadb
from chromadb.utils import embedding_functions
import logging
import argparse

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize ChromaDB client and collection
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(
    name="backtest_results",
    embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
)

def query_collection(symbol: str = None, min_profit: float = None, limit: int = None):
    """Query the ChromaDB collection with optional filters."""
    # Base query to get all entries
    query_params = {"include": ["documents", "metadatas", "ids"]}
    
    # Apply filters if provided
    if symbol:
        query_params["where"] = {"symbol": symbol}
    
    # Get results
    results = collection.get(**query_params)
    
    # Filter by minimum profit (if specified) since 'where' doesn't support numerical comparisons
    if min_profit is not None:
        filtered_results = {
            "ids": [],
            "documents": [],
            "metadatas": []
        }
        for i, meta in enumerate(results["metadatas"]):
            if meta.get("total_profit", 0) >= min_profit:
                filtered_results["ids"].append(results["ids"][i])
                filtered_results["documents"].append(results["documents"][i])
                filtered_results["metadatas"].append(results["metadatas"][i])
        results = filtered_results

    # Apply limit if specified
    if limit is not None:
        results["ids"] = results["ids"][:limit]
        results["documents"] = results["documents"][:limit]
        results["metadatas"] = results["metadatas"][:limit]

    # Display results
    if not results["ids"]:
        print("No entries found matching the criteria.")
    else:
        print(f"Found {len(results['ids'])} entries matching the criteria.\n")
        for i, (doc, meta, id_) in enumerate(zip(results["documents"], results["metadatas"], results["ids"]), 1):
            print(f"Entry {i}:")
            print(f"ID: {id_}")
            print(f"Document: {doc}")
            print("Metadata:")
            for key, value in meta.items():
                print(f"  {key}: {value}")
            print("\n" + "-"*50 + "\n")

if __name__ == "__main__":
    # Set up argument parser for command-line options
    parser = argparse.ArgumentParser(description="Query ChromaDB backtest_results collection.")
    parser.add_argument("--symbol", type=str, help="Filter by stock symbol (e.g., NVDA)")
    parser.add_argument("--min-profit", type=float, help="Filter by minimum total profit")
    parser.add_argument("--limit", type=int, help="Limit the number of results")
    args = parser.parse_args()

    # Run the query with provided arguments
    query_collection(symbol=args.symbol, min_profit=args.min_profit, limit=args.limit)