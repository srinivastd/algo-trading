/* Copyright (C) 2025 Interactive Brokers LLC. All rights reserved. This code is subject to the terms
 * and conditions of the IB API Non-Commercial License or the IB API Commercial License, as applicable. */

#pragma once
#ifndef TWS_API_CLIENT_EDECODER_UTILS_H
#define TWS_API_CLIENT_EDECODER_UTILS_H

#include "Contract.h"
#include "Execution.h"
#include "ExecutionDetails.pb.h"

class EDecoderUtils {

public:
    static Contract decodeContract(protobuf::Contract contractProto);
    static Execution decodeExecution(protobuf::Execution executionProto);
};

#endif

