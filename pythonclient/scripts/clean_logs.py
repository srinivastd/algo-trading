#!/usr/bin/env python3
"""
Log Cleaner Script
Filters logs to keep only market data and trading signals, removes duplicates and unnecessary logs.
"""

import json
import os
import argparse
from datetime import datetime
from typing import Set, Dict, List
import hashlib

class LogCleaner:
    def __init__(self):
        self.seen_hashes: Set[str] = set()
        self.kept_logs: List[Dict] = []
        
    def is_relevant_log(self, log_entry: Dict) -> bool:
        """Check if a log entry is relevant (market data or trading signals)"""
        # Check if it has the structure of a market data log
        if not isinstance(log_entry, dict):
            return False
            
        # Must have timestamp and either symbol or message
        if 'timestamp' not in log_entry:
            return False
            
        # Keep system messages related to trading
        if log_entry.get('type') == 'system':
            message = log_entry.get('message', '').lower()
            if any(keyword in message for keyword in ['watch mode', 'strategy', 'position', 'trade']):
                return True
            return False
        
        # Keep entries with symbols (market data)
        if 'symbol' in log_entry:
            action = log_entry.get('action', '')
            
            # Keep all market data and trading related actions
            relevant_actions = [
                'trade_execution_attempt',
                'trade_execution_result', 
                'position_monitoring',
                'position_exit_attempt',
                'position_exit_result'
            ]
            
            # Keep if it has bar_data (raw market data) or signal data
            if 'bar_data' in log_entry or 'signal' in log_entry:
                return True
                
            # Keep if it's a relevant trading action
            if action in relevant_actions:
                return True
                
            # Keep if it has market_context (signal generation)
            if 'market_context' in log_entry:
                return True
        
        return False
    
    def get_log_hash(self, log_entry: Dict) -> str:
        """Generate a hash for duplicate detection"""
        # Create a simplified version for hashing
        hash_data = {
            'timestamp': log_entry.get('timestamp', ''),
            'symbol': log_entry.get('symbol', ''),
            'action': log_entry.get('action', ''),
            'signal': log_entry.get('signal', 0)
        }
        
        # For bar data, include price to distinguish different bars
        if 'bar_data' in log_entry:
            hash_data['close_price'] = log_entry['bar_data'].get('close', 0)
            
        # For trade data, include key trade details
        if 'trade_details' in log_entry:
            hash_data['entry_price'] = log_entry['trade_details'].get('entry_price', 0)
            hash_data['shares'] = log_entry['trade_details'].get('shares', 0)
        
        hash_string = json.dumps(hash_data, sort_keys=True)
        return hashlib.md5(hash_string.encode()).hexdigest()
    
    def is_duplicate(self, log_entry: Dict) -> bool:
        """Check if this log entry is a duplicate"""
        log_hash = self.get_log_hash(log_entry)
        if log_hash in self.seen_hashes:
            return True
        self.seen_hashes.add(log_hash)
        return False
    
    def clean_market_data_logs(self, input_file: str, output_file: str = None) -> Dict:
        """Clean market data JSON logs"""
        if output_file is None:
            base, ext = os.path.splitext(input_file)
            output_file = f"{base}_cleaned{ext}"
        
        stats = {
            'total_lines': 0,
            'relevant_lines': 0,
            'duplicate_lines': 0,
            'kept_lines': 0
        }
        
        print(f"Processing {input_file}...")
        
        with open(input_file, 'r') as infile:
            for line_num, line in enumerate(infile, 1):
                stats['total_lines'] += 1
                
                # Skip empty lines
                line = line.strip()
                if not line:
                    continue
                
                try:
                    log_entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                
                # Check if relevant
                if not self.is_relevant_log(log_entry):
                    continue
                    
                stats['relevant_lines'] += 1
                
                # Check for duplicates
                if self.is_duplicate(log_entry):
                    stats['duplicate_lines'] += 1
                    continue
                
                # Keep this log
                self.kept_logs.append(log_entry)
                stats['kept_lines'] += 1
        
        # Write cleaned logs
        with open(output_file, 'w') as outfile:
            for log_entry in self.kept_logs:
                outfile.write(json.dumps(log_entry) + '\n')
        
        print(f"✅ Cleaned logs written to {output_file}")
        return stats
    
    def clean_text_logs(self, input_file: str, output_file: str = None) -> Dict:
        """Clean text-based log files (like stock_analysis.log)"""
        if output_file is None:
            base, ext = os.path.splitext(input_file)
            output_file = f"{base}_cleaned{ext}"
        
        stats = {
            'total_lines': 0,
            'kept_lines': 0
        }
        
        # Keywords to keep in text logs
        keep_keywords = [
            'signal', 'trade', 'position', 'buy', 'sell', 'entry', 'exit',
            'executed', 'failed', 'closed', 'profit', 'loss', 'pnl'
        ]
        
        # Keywords to exclude (debug/noise)
        exclude_keywords = [
            'debug', 'waiting for more data', 'data status', 'outside market hours',
            'requested real-time bars', 'received new bar', 'qualifying contract'
        ]
        
        kept_lines = []
        
        print(f"Processing {input_file}...")
        
        with open(input_file, 'r') as infile:
            for line in infile:
                stats['total_lines'] += 1
                line_lower = line.lower()
                
                # Skip if contains exclude keywords
                if any(keyword in line_lower for keyword in exclude_keywords):
                    continue
                
                # Keep if contains keep keywords or is ERROR/WARNING about trading
                if (any(keyword in line_lower for keyword in keep_keywords) or 
                    ('error' in line_lower and any(word in line_lower for word in ['trade', 'order', 'position'])) or
                    ('warning' in line_lower and any(word in line_lower for word in ['trade', 'order', 'position']))):
                    kept_lines.append(line)
                    stats['kept_lines'] += 1
        
        # Write cleaned logs
        with open(output_file, 'w') as outfile:
            outfile.writelines(kept_lines)
        
        print(f"✅ Cleaned text logs written to {output_file}")
        return stats

def main():
    parser = argparse.ArgumentParser(description='Clean trading logs to keep only market data and signals')
    parser.add_argument('--market-data', type=str, default='market_data_logs/market_data.json',
                      help='Path to market data JSON log file')
    parser.add_argument('--text-logs', type=str, default='stock_analysis.log',
                      help='Path to text log file')
    parser.add_argument('--output-dir', type=str, default='cleaned_logs',
                      help='Directory for cleaned log files')
    parser.add_argument('--dry-run', action='store_true',
                      help='Show what would be cleaned without writing files')
    
    args = parser.parse_args()
    
    # Create output directory
    if not args.dry_run:
        os.makedirs(args.output_dir, exist_ok=True)
    
    cleaner = LogCleaner()
    total_stats = {}
    
    # Clean market data logs if file exists
    if os.path.exists(args.market_data):
        output_file = None if args.dry_run else os.path.join(args.output_dir, 'market_data_cleaned.json')
        if not args.dry_run:
            stats = cleaner.clean_market_data_logs(args.market_data, output_file)
            total_stats['market_data'] = stats
        else:
            print(f"Would process: {args.market_data}")
    else:
        print(f"⚠️  Market data file not found: {args.market_data}")
    
    # Clean text logs if file exists  
    if os.path.exists(args.text_logs):
        output_file = None if args.dry_run else os.path.join(args.output_dir, 'analysis_cleaned.log')
        if not args.dry_run:
            stats = cleaner.clean_text_logs(args.text_logs, output_file)
            total_stats['text_logs'] = stats
        else:
            print(f"Would process: {args.text_logs}")
    else:
        print(f"⚠️  Text log file not found: {args.text_logs}")
    
    # Print summary
    if not args.dry_run and total_stats:
        print("\n" + "="*50)
        print("CLEANING SUMMARY")
        print("="*50)
        
        for log_type, stats in total_stats.items():
            print(f"\n{log_type.upper()}:")
            if 'relevant_lines' in stats:
                print(f"  Total lines: {stats['total_lines']:,}")
                print(f"  Relevant lines: {stats['relevant_lines']:,}")
                print(f"  Duplicate lines: {stats['duplicate_lines']:,}")
                print(f"  Kept lines: {stats['kept_lines']:,}")
                reduction = (1 - stats['kept_lines'] / stats['total_lines']) * 100
                print(f"  Size reduction: {reduction:.1f}%")
            else:
                print(f"  Total lines: {stats['total_lines']:,}")
                print(f"  Kept lines: {stats['kept_lines']:,}")
                reduction = (1 - stats['kept_lines'] / stats['total_lines']) * 100
                print(f"  Size reduction: {reduction:.1f}%")
        
        print(f"\n✅ Cleaned logs saved to: {args.output_dir}/")

if __name__ == "__main__":
    main() 