"""
Backtest vs Paper Trading Comparison Tool
Analyzes differences between backtesting and paper trading results
to identify potential issues and optimization opportunities.
"""

import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import matplotlib.pyplot as plt
import seaborn as sns

logger = logging.getLogger(__name__)

class BacktestPaperComparison:
    """Compare backtesting vs paper trading results"""
    
    def __init__(self):
        self.comparison_results = {}
        self.discrepancy_analysis = {}
        
    def compare_results(self, backtest_results: Dict, paper_results: Dict, symbol: str) -> Dict:
        """
        Compare backtesting vs paper trading results
        
        Args:
            backtest_results: Results from backtesting
            paper_results: Results from paper trading
            symbol: Trading symbol
            
        Returns:
            Dictionary with comparison analysis
        """
        comparison = {
            'symbol': symbol,
            'timestamp': datetime.now(),
            'metrics_comparison': {},
            'discrepancies': [],
            'recommendations': []
        }
        
        # Compare key metrics
        metrics = ['total_profit', 'return_pct', 'success_rate', 'total_trades']
        
        for metric in metrics:
            backtest_value = backtest_results.get(metric, 0)
            paper_value = paper_results.get(metric, 0)
            
            if backtest_value != 0:
                difference = ((paper_value - backtest_value) / backtest_value) * 100
            else:
                difference = 0
            
            comparison['metrics_comparison'][metric] = {
                'backtest': backtest_value,
                'paper': paper_value,
                'difference_pct': difference,
                'significant_difference': abs(difference) > 10  # 10% threshold
            }
        
        # Analyze discrepancies
        discrepancies = []
        
        # Check for significant profit differences
        profit_diff = comparison['metrics_comparison']['total_profit']['difference_pct']
        if abs(profit_diff) > 20:  # 20% threshold for profit
            discrepancies.append({
                'type': 'profit_discrepancy',
                'severity': 'high' if abs(profit_diff) > 50 else 'medium',
                'description': f"Profit difference of {profit_diff:.1f}% between backtest and paper trading",
                'backtest_profit': backtest_results.get('total_profit', 0),
                'paper_profit': paper_results.get('total_profit', 0)
            })
        
        # Check for success rate differences
        success_diff = comparison['metrics_comparison']['success_rate']['difference_pct']
        if abs(success_diff) > 15:  # 15% threshold for success rate
            discrepancies.append({
                'type': 'success_rate_discrepancy',
                'severity': 'high' if abs(success_diff) > 30 else 'medium',
                'description': f"Success rate difference of {success_diff:.1f}% between backtest and paper trading",
                'backtest_success': backtest_results.get('success_rate', 0),
                'paper_success': paper_results.get('success_rate', 0)
            })
        
        # Check for trade count differences
        trade_diff = comparison['metrics_comparison']['total_trades']['difference_pct']
        if abs(trade_diff) > 25:  # 25% threshold for trade count
            discrepancies.append({
                'type': 'trade_count_discrepancy',
                'severity': 'medium',
                'description': f"Trade count difference of {trade_diff:.1f}% between backtest and paper trading",
                'backtest_trades': backtest_results.get('total_trades', 0),
                'paper_trades': paper_results.get('total_trades', 0)
            })
        
        comparison['discrepancies'] = discrepancies
        
        # Generate recommendations
        recommendations = []
        
        if discrepancies:
            recommendations.append("Investigate data quality issues in real-time feeds")
            recommendations.append("Check for slippage and execution delays")
            recommendations.append("Verify signal generation timing consistency")
            
            if any(d['type'] == 'profit_discrepancy' and d['severity'] == 'high' for d in discrepancies):
                recommendations.append("Consider adjusting risk management parameters")
                recommendations.append("Review position sizing logic")
        
        if profit_diff < -30:  # Paper trading significantly underperforming
            recommendations.append("Paper trading underperforming - check execution quality")
            recommendations.append("Consider reducing position sizes in paper trading")
        
        if profit_diff > 30:  # Paper trading significantly outperforming
            recommendations.append("Paper trading outperforming - verify backtest assumptions")
            recommendations.append("Check for overfitting in backtest parameters")
        
        comparison['recommendations'] = recommendations
        
        # Store comparison
        self.comparison_results[symbol] = comparison
        
        return comparison
    
    def analyze_trade_level_discrepancies(self, backtest_trades: List, paper_trades: List) -> Dict:
        """
        Analyze discrepancies at the individual trade level
        
        Args:
            backtest_trades: List of backtest trades
            paper_trades: List of paper trading trades
            
        Returns:
            Dictionary with trade-level analysis
        """
        analysis = {
            'trade_count_comparison': {
                'backtest_trades': len(backtest_trades),
                'paper_trades': len(paper_trades),
                'difference': len(backtest_trades) - len(paper_trades)
            },
            'profit_comparison': {
                'backtest_total_profit': sum(t.get('profit', 0) for t in backtest_trades),
                'paper_total_profit': sum(t.get('profit', 0) for t in paper_trades),
                'profit_difference': sum(t.get('profit', 0) for t in backtest_trades) - sum(t.get('profit', 0) for t in paper_trades)
            },
            'missing_trades': [],
            'execution_differences': []
        }
        
        # Find missing trades (trades in backtest but not in paper trading)
        backtest_trade_ids = set()
        paper_trade_ids = set()
        
        for trade in backtest_trades:
            trade_id = f"{trade.get('entry_date')}_{trade.get('type')}_{trade.get('shares')}"
            backtest_trade_ids.add(trade_id)
        
        for trade in paper_trades:
            trade_id = f"{trade.get('entry_date')}_{trade.get('type')}_{trade.get('shares')}"
            paper_trade_ids.add(trade_id)
        
        missing_in_paper = backtest_trade_ids - paper_trade_ids
        missing_in_backtest = paper_trade_ids - backtest_trade_ids
        
        analysis['missing_trades'] = {
            'missing_in_paper': len(missing_in_paper),
            'missing_in_backtest': len(missing_in_backtest),
            'total_discrepancy': len(missing_in_paper) + len(missing_in_backtest)
        }
        
        # Analyze execution differences for matching trades
        for backtest_trade in backtest_trades:
            backtest_id = f"{backtest_trade.get('entry_date')}_{backtest_trade.get('type')}_{backtest_trade.get('shares')}"
            
            # Find corresponding paper trade
            matching_paper_trade = None
            for paper_trade in paper_trades:
                paper_id = f"{paper_trade.get('entry_date')}_{paper_trade.get('type')}_{paper_trade.get('shares')}"
                if backtest_id == paper_id:
                    matching_paper_trade = paper_trade
                    break
            
            if matching_paper_trade:
                # Compare execution prices
                backtest_entry = backtest_trade.get('entry_price', 0)
                paper_entry = matching_paper_trade.get('entry_price', 0)
                
                if backtest_entry > 0 and paper_entry > 0:
                    price_diff_pct = abs(backtest_entry - paper_entry) / backtest_entry * 100
                    
                    if price_diff_pct > 0.1:  # 0.1% threshold
                        analysis['execution_differences'].append({
                            'trade_id': backtest_id,
                            'backtest_entry': backtest_entry,
                            'paper_entry': paper_entry,
                            'price_difference_pct': price_diff_pct,
                            'entry_date': backtest_trade.get('entry_date')
                        })
        
        return analysis
    
    def generate_comparison_report(self, symbol: str = None) -> str:
        """
        Generate a comprehensive comparison report
        
        Args:
            symbol: Specific symbol or None for all symbols
            
        Returns:
            Report text
        """
        if symbol:
            comparisons = [self.comparison_results.get(symbol, {})]
        else:
            comparisons = list(self.comparison_results.values())
        
        if not comparisons:
            return "No comparison data available"
        
        report = []
        report.append("=" * 60)
        report.append("BACKTEST vs PAPER TRADING COMPARISON REPORT")
        report.append("=" * 60)
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        for comparison in comparisons:
            symbol = comparison.get('symbol', 'Unknown')
            report.append(f"Symbol: {symbol}")
            report.append("-" * 40)
            
            # Metrics comparison
            metrics = comparison.get('metrics_comparison', {})
            for metric, data in metrics.items():
                report.append(f"{metric.replace('_', ' ').title()}:")
                report.append(f"  Backtest: {data['backtest']:.2f}")
                report.append(f"  Paper:    {data['paper']:.2f}")
                report.append(f"  Difference: {data['difference_pct']:+.1f}%")
                if data['significant_difference']:
                    report.append(f"  ⚠️  SIGNIFICANT DIFFERENCE DETECTED")
                report.append("")
            
            # Discrepancies
            discrepancies = comparison.get('discrepancies', [])
            if discrepancies:
                report.append("DISCREPANCIES FOUND:")
                for disc in discrepancies:
                    report.append(f"  {disc['severity'].upper()}: {disc['description']}")
                report.append("")
            
            # Recommendations
            recommendations = comparison.get('recommendations', [])
            if recommendations:
                report.append("RECOMMENDATIONS:")
                for rec in recommendations:
                    report.append(f"  • {rec}")
                report.append("")
            
            report.append("=" * 60)
            report.append("")
        
        return "\n".join(report)
    
    def plot_comparison_charts(self, symbol: str, save_path: str = None):
        """
        Create visualization charts for comparison
        
        Args:
            symbol: Trading symbol
            save_path: Path to save charts (optional)
        """
        if symbol not in self.comparison_results:
            logger.warning(f"No comparison data for symbol {symbol}")
            return
        
        comparison = self.comparison_results[symbol]
        metrics = comparison.get('metrics_comparison', {})
        
        # Create subplots
        fig, axes = plt.subplots(2, 2, figsize=(15, 10))
        fig.suptitle(f'Backtest vs Paper Trading Comparison - {symbol}', fontsize=16)
        
        # Profit comparison
        if 'total_profit' in metrics:
            data = metrics['total_profit']
            axes[0, 0].bar(['Backtest', 'Paper'], [data['backtest'], data['paper']], 
                          color=['blue', 'orange'])
            axes[0, 0].set_title('Total Profit Comparison')
            axes[0, 0].set_ylabel('Profit ($)')
            axes[0, 0].text(0.5, 0.9, f"Diff: {data['difference_pct']:+.1f}%", 
                           transform=axes[0, 0].transAxes, ha='center')
        
        # Return percentage comparison
        if 'return_pct' in metrics:
            data = metrics['return_pct']
            axes[0, 1].bar(['Backtest', 'Paper'], [data['backtest'], data['paper']], 
                          color=['blue', 'orange'])
            axes[0, 1].set_title('Return Percentage Comparison')
            axes[0, 1].set_ylabel('Return (%)')
            axes[0, 1].text(0.5, 0.9, f"Diff: {data['difference_pct']:+.1f}%", 
                           transform=axes[0, 1].transAxes, ha='center')
        
        # Success rate comparison
        if 'success_rate' in metrics:
            data = metrics['success_rate']
            axes[1, 0].bar(['Backtest', 'Paper'], [data['backtest'], data['paper']], 
                          color=['blue', 'orange'])
            axes[1, 0].set_title('Success Rate Comparison')
            axes[1, 0].set_ylabel('Success Rate (%)')
            axes[1, 0].text(0.5, 0.9, f"Diff: {data['difference_pct']:+.1f}%", 
                           transform=axes[1, 0].transAxes, ha='center')
        
        # Trade count comparison
        if 'total_trades' in metrics:
            data = metrics['total_trades']
            axes[1, 1].bar(['Backtest', 'Paper'], [data['backtest'], data['paper']], 
                          color=['blue', 'orange'])
            axes[1, 1].set_title('Total Trades Comparison')
            axes[1, 1].set_ylabel('Number of Trades')
            axes[1, 1].text(0.5, 0.9, f"Diff: {data['difference_pct']:+.1f}%", 
                           transform=axes[1, 1].transAxes, ha='center')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            logger.info(f"Comparison charts saved to {save_path}")
        
        plt.show()


# Global instance for easy access
comparison_tool = BacktestPaperComparison() 