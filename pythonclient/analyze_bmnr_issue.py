#!/usr/bin/env python3
"""
Analyze why BMNR strategy is underperforming compared to QBTS
"""

import chromadb
from chromadb.utils import embedding_functions

def analyze_bmnr_issue():
    chroma_client = chromadb.PersistentClient(path='./chroma_db')
    collection = chroma_client.get_or_create_collection(
        name='backtest_results',
        embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name='all-MiniLM-L6-v2')
    )

    results = collection.get(include=['metadatas'])

    print('=== BMNR vs QBTS STRATEGY ANALYSIS ===')

    # Get latest runs for each symbol
    bmnr_data = None
    qbts_data = None
    ids = collection.get()['ids']

    # Find most recent BMNR and QBTS runs
    bmnr_runs = []
    qbts_runs = []

    for i, meta in enumerate(results['metadatas']):
        symbol = meta.get('symbol', '').lower()
        if symbol == 'bmnr':
            bmnr_runs.append((ids[i], meta))
        elif symbol == 'qbts':
            qbts_runs.append((ids[i], meta))

    # Sort by timestamp and get most recent
    def get_timestamp(run_id):
        try:
            return run_id.split('_', 1)[1]
        except:
            return '0000'

    if bmnr_runs:
        bmnr_runs.sort(key=lambda x: get_timestamp(x[0]), reverse=True)
        bmnr_data = bmnr_runs[0][1]  # Most recent BMNR

    if qbts_runs:
        qbts_runs.sort(key=lambda x: get_timestamp(x[0]), reverse=True)
        qbts_data = qbts_runs[0][1]  # Most recent QBTS

    if not bmnr_data or not qbts_data:
        print("❌ Missing data for BMNR or QBTS")
        return

    print('\n📊 MARKET CONDITIONS:')
    print(f'BMNR - ADX: {bmnr_data.get("adx", 0):.1f}, Volatility: {bmnr_data.get("volatility", 0):.3f}')
    print(f'QBTS - ADX: {qbts_data.get("adx", 0):.1f}, Volatility: {qbts_data.get("volatility", 0):.3f}')

    print('\n💰 PERFORMANCE:')
    bmnr_return = (bmnr_data.get('final_capital', 0) - bmnr_data.get('initial_capital', 0)) / bmnr_data.get('initial_capital', 1) * 100
    qbts_return = (qbts_data.get('final_capital', 0) - qbts_data.get('initial_capital', 0)) / qbts_data.get('initial_capital', 1) * 100
    print(f'BMNR: {bmnr_return:+.1f}% (${bmnr_data.get("total_profit", 0):,.2f}) - {bmnr_data.get("total_trades", 0)} trades')
    print(f'QBTS: {qbts_return:+.1f}% (${qbts_data.get("total_profit", 0):,.2f}) - {qbts_data.get("total_trades", 0)} trades')

    print('\n📈 TECHNICAL INDICATORS:')
    factors = ['rsi', 'macd_histogram', 'bb_position', 'volume_ratio', 'returns_20d', 'stock_profile_score']
    for factor in factors:
        bmnr_val = bmnr_data.get(factor)
        qbts_val = qbts_data.get(factor)
        if bmnr_val is not None and qbts_val is not None:
            print(f'{factor}: BMNR {bmnr_val:.3f}, QBTS {qbts_val:.3f}')
        elif bmnr_val is not None:
            print(f'{factor}: BMNR {bmnr_val:.3f}, QBTS N/A')
        elif qbts_val is not None:
            print(f'{factor}: BMNR N/A, QBTS {qbts_val:.3f}')

    print('\n🔍 ANALYSIS:')

    # Volatility analysis
    bmnr_vol = bmnr_data.get("volatility", 0)
    qbts_vol = qbts_data.get("volatility", 0)

    if bmnr_vol > qbts_vol * 1.5:
        print('❌ HIGH VOLATILITY: BMNR volatility is significantly higher, causing noisy signals')
        print('   Strategy relies on zone breakouts, but high volatility creates false breakouts')

    # ADX analysis
    bmnr_adx = bmnr_data.get("adx", 0)
    qbts_adx = qbts_data.get("adx", 0)

    if bmnr_adx < 25:
        print('⚠️  WEAK TREND: BMNR ADX indicates weak trend strength')
        print('   Strategy performs better in trending markets (ADX > 25)')

    # Profile score analysis
    bmnr_profile = bmnr_data.get("stock_profile_score")
    qbts_profile = qbts_data.get("stock_profile_score")

    if bmnr_profile is not None and qbts_profile is not None and bmnr_profile < qbts_profile:
        print('📉 POOR PROFILE: BMNR has worse stock profile score')
        print('   Lower trend strength, momentum, and technical alignment')

    print('\n💡 RECOMMENDATIONS:')
    print('1. 🛑 AVOID HIGH VOLATILITY STOCKS: BMNR\'s 3.018 volatility is too high for zone-based strategy')
    print('2. 🎯 TARGET TRENDING STOCKS: Look for stocks with ADX > 25 and volatility < 2.0')
    print('3. 📊 ADD VOLATILITY FILTERS: Skip stocks with volatility > 2x average market volatility')
    print('4. 🔄 CONSIDER ALTERNATIVE STRATEGIES: Mean reversion or options strategies for high-vol stocks')

if __name__ == "__main__":
    analyze_bmnr_issue()
