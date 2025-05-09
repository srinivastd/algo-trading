/* Copyright (C) 2025 Interactive Brokers LLC. All rights reserved. This code is subject to the terms
 * and conditions of the IB API Non-Commercial License or the IB API Commercial License, as applicable. */

#include "StdAfx.h"
#include "Utils.h"
#include "EDecoderUtils.h"

Contract EDecoderUtils::decodeContract(protobuf::Contract contractProto) {
    Contract contract;
    if (contractProto.has_conid()) {
        contract.conId = contractProto.conid();
    }
    if (contractProto.has_symbol()) {
        contract.symbol = contractProto.symbol();
    }
    if (contractProto.has_sectype()) {
        contract.secType = contractProto.sectype();
    }
    if (contractProto.has_lasttradedateorcontractmonth()) {
        contract.lastTradeDateOrContractMonth = contractProto.lasttradedateorcontractmonth();
    }
    if (contractProto.has_strike()) {
        contract.strike = contractProto.strike();
    }
    if (contractProto.has_right()) {
        contract.right = contractProto.right();
    }
    if (contractProto.has_multiplier()) {
        contract.multiplier = std::to_string(contractProto.multiplier());
    }
    if (contractProto.has_exchange()) {
        contract.exchange = contractProto.exchange();
    }
    if (contractProto.has_currency()) {
        contract.currency = contractProto.currency();
    }
    if (contractProto.has_localsymbol()) {
        contract.localSymbol = contractProto.localsymbol();
    }
    if (contractProto.has_tradingclass()) {
        contract.tradingClass = contractProto.tradingclass();
    }
    return contract;
}

Execution EDecoderUtils::decodeExecution(protobuf::Execution executionProto) {
    Execution execution;
    if (executionProto.has_orderid()) {
        execution.orderId = executionProto.orderid();
    }
    if (executionProto.has_clientid()) {
        execution.clientId = executionProto.clientid();
    }
    if (executionProto.has_execid()) {
        execution.execId = executionProto.execid();
    }
    if (executionProto.has_time()) {
        execution.time = executionProto.time();
    }
    if (executionProto.has_acctnumber()) {
        execution.acctNumber = executionProto.acctnumber();
    }
    if (executionProto.has_exchange()) {
        execution.exchange = executionProto.exchange();
    }
    if (executionProto.has_side()) {
        execution.side = executionProto.side();
    }
    if (executionProto.has_shares()) {
        execution.shares = DecimalFunctions::stringToDecimal(executionProto.shares());
    }
    if (executionProto.has_price()) {
        execution.price = executionProto.price();
    }
    if (executionProto.has_permid()) {
        execution.permId = executionProto.permid();
    }
    if (executionProto.has_isliquidation()) {
        execution.liquidation = executionProto.isliquidation() ? 1 : 0;
    }
    if (executionProto.has_cumqty()) {
        execution.cumQty = DecimalFunctions::stringToDecimal(executionProto.cumqty());
    }
    if (executionProto.has_avgprice()) {
        execution.avgPrice = executionProto.avgprice();
    }
    if (executionProto.has_orderref()) {
        execution.orderRef = executionProto.orderref();
    }
    if (executionProto.has_evrule()) {
        execution.evRule = executionProto.evrule();
    }
    if (executionProto.has_evmultiplier()) {
        execution.evMultiplier = executionProto.evmultiplier();
    }
    if (executionProto.has_modelcode()) {
        execution.modelCode = executionProto.modelcode();
    }
    if (executionProto.has_lastliquidity()) {
        execution.lastLiquidity = executionProto.lastliquidity();
    }
    if (executionProto.has_ispricerevisionpending()) {
        execution.pendingPriceRevision = executionProto.ispricerevisionpending();
    }
    if (executionProto.has_submitter()) {
        execution.submitter = executionProto.submitter();
    }
    return execution;
}

