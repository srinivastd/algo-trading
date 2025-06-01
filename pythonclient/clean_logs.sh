#!/bin/bash

# Log Cleaner - Quick Usage Script
# Cleans trading logs to keep only market data and trading signals

echo "🧹 Trading Log Cleaner"
echo "====================="

# Make scripts directory if it doesn't exist
mkdir -p scripts

# Check if the python script exists
if [ ! -f "scripts/clean_logs.py" ]; then
    echo "❌ clean_logs.py not found in scripts/ directory"
    exit 1
fi

# Make the python script executable
chmod +x scripts/clean_logs.py

# Default options
MARKET_DATA_FILE="market_data_logs/market_data.json"
TEXT_LOG_FILE="stock_analysis.log"
OUTPUT_DIR="cleaned_logs"

# Check command line arguments
case "${1:-run}" in
    "dry-run"|"preview")
        echo "🔍 Preview mode - showing what would be cleaned..."
        python3 scripts/clean_logs.py --dry-run --market-data "$MARKET_DATA_FILE" --text-logs "$TEXT_LOG_FILE"
        ;;
    "run"|"clean")
        echo "🧹 Cleaning logs..."
        python3 scripts/clean_logs.py --market-data "$MARKET_DATA_FILE" --text-logs "$TEXT_LOG_FILE" --output-dir "$OUTPUT_DIR"
        ;;
    "help"|"-h"|"--help")
        echo "Usage: $0 [command]"
        echo ""
        echo "Commands:"
        echo "  run, clean     Clean the logs (default)"
        echo "  dry-run        Preview what would be cleaned"
        echo "  help           Show this help"
        echo ""
        echo "Files processed:"
        echo "  - $MARKET_DATA_FILE"
        echo "  - $TEXT_LOG_FILE"
        echo ""
        echo "Output directory: $OUTPUT_DIR"
        ;;
    *)
        echo "❌ Unknown command: $1"
        echo "Use '$0 help' for usage information"
        exit 1
        ;;
esac 