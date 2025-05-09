/* Copyright (C) 2025 Interactive Brokers LLC. All rights reserved. This code is subject to the terms
 * and conditions of the IB API Non-Commercial License or the IB API Commercial License, as applicable. */

package com.ib.client;

import com.ib.client.protobuf.ContractProto;
import com.ib.client.protobuf.ExecutionProto;

public class EDecoderUtils {

    public static Contract decodeContract(ContractProto.Contract contractProto) {
        Contract contract = new Contract();
        if (contractProto.hasConId()) {
            contract.conid(contractProto.getConId());
        }
        if (contractProto.hasSymbol()) {
            contract.symbol(contractProto.getSymbol());
        }
        if (contractProto.hasSecType()) {
            contract.secType(contractProto.getSecType());
        }
        if (contractProto.hasLastTradeDateOrContractMonth()) {
            contract.lastTradeDateOrContractMonth(contractProto.getLastTradeDateOrContractMonth());
        }
        if (contractProto.hasStrike()) {
            contract.strike(contractProto.getStrike());
        }
        if (contractProto.hasRight()) {
            contract.right(contractProto.getRight());
        }
        if (contractProto.hasMultiplier()) {
            contract.multiplier(String.valueOf(contractProto.getMultiplier()));
        }
        if (contractProto.hasExchange()) {
            contract.exchange(contractProto.getExchange());
        }
        if (contractProto.hasCurrency()) {
            contract.currency(contractProto.getCurrency());
        }
        if (contractProto.hasLocalSymbol()) {
            contract.localSymbol(contractProto.getLocalSymbol());
        }
        if (contractProto.hasTradingClass()) {
            contract.tradingClass(contractProto.getTradingClass());
        }
        return contract;
    }

    public static Execution decodeExecution(ExecutionProto.Execution executionProto) {
        Execution execution = new Execution();
        if (executionProto.hasOrderId()) {
            execution.orderId(executionProto.getOrderId());
        }
        if (executionProto.hasClientId()) {
            execution.clientId(executionProto.getClientId());
        }
        if (executionProto.hasExecId()) {
            execution.execId(executionProto.getExecId());
        }
        if (executionProto.hasTime()) {
            execution.time(executionProto.getTime());
        }
        if (executionProto.hasAcctNumber()) {
            execution.acctNumber(executionProto.getAcctNumber());
        }
        if (executionProto.hasExchange()) {
            execution.exchange(executionProto.getExchange());
        }
        if (executionProto.hasSide()) {
            execution.side(executionProto.getSide());
        }
        if (executionProto.hasShares()) {
            execution.shares(Util.stringToDecimal(executionProto.getShares()));
        }
        if (executionProto.hasPrice()) {
            execution.price(executionProto.getPrice());
        }
        if (executionProto.hasPermId()) {
            execution.permId(executionProto.getPermId());
        }
        if (executionProto.hasIsLiquidation()) {
            execution.liquidation(executionProto.getIsLiquidation() ? 1 : 0);
        }
        if (executionProto.hasCumQty()) {
            execution.cumQty(Util.stringToDecimal(executionProto.getCumQty()));
        }
        if (executionProto.hasAvgPrice()) {
            execution.avgPrice(executionProto.getAvgPrice());
        }
        if (executionProto.hasOrderRef()) {
            execution.orderRef(executionProto.getOrderRef());
        }
        if (executionProto.hasEvRule()) {
            execution.evRule(executionProto.getEvRule());
        }
        if (executionProto.hasEvMultiplier()) {
            execution.evMultiplier(executionProto.getEvMultiplier());
        }
        if (executionProto.hasModelCode()) {
            execution.modelCode(executionProto.getModelCode());
        }
        if (executionProto.hasLastLiquidity()) {
            execution.lastLiquidity(executionProto.getLastLiquidity());
        }
        if (executionProto.hasIsPriceRevisionPending()) {
            execution.pendingPriceRevision(executionProto.getIsPriceRevisionPending());
        }
        if (executionProto.hasSubmitter()) {
            execution.submitter(executionProto.getSubmitter());
        }
        return execution;
    }
}
