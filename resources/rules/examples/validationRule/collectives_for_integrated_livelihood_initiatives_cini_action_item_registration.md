---
rule_kind: validationRule
intent: block scheduling a new follow-up of one type while a different follow-up type
  is still open (must be cancelled first)
entity_param: individual
encounter_types: []
concepts:
- 7ea54c7a-c857-4c7b-b403-e81bfaa762e1
source_org: Collectives for Integrated Livelihood Initiatives (CInI)
---
```js
'use strict';
 ({params, imports}) => {
  const individual = params.entity;
  const validationResults = [];
  
  const isGrievanceFollowUpForFCAlreadyScheduled = individual.getEncounters().filter(encounter => 
  encounter.name === 'Action Item Follow Up for Field Coordinator (FC)' && encounter.earliestVisitDateTime && !encounter.cancelDateTime
  ).length > 0;
  
  const isGrievanceFollowUpForCCAlreadyScheduled = individual.getEncounters().filter(encounter => 
  encounter.name === 'Action Item Follow Up for Cluster Coordinator (CC)' && encounter.earliestVisitDateTime && !encounter.cancelDateTime
  ).length > 0;
  
  if (individual.getObservationReadableValue('7ea54c7a-c857-4c7b-b403-e81bfaa762e1') === 'SMC' && isGrievanceFollowUpForCCAlreadyScheduled) {
  validationResults.push(imports.common.createValidationError('Please ensure to cancel the "Action Item Follow Up for Field Coordinator (FC)" visit before scheduling the "Action Item Follow Up for Cluster Coordinator (CC)" visit'));
  }
  
  if (individual.getObservationReadableValue('7ea54c7a-c857-4c7b-b403-e81bfaa762e1') === 'SMC CG' && isGrievanceFollowUpForFCAlreadyScheduled) {
  validationResults.push(imports.common.createValidationError('Please ensure to cancel the "Action Item Follow Up for Cluster Coordinator (CC)" visit before scheduling the "Action Item Follow Up for Field Coordinator (FC)" visit'));
  }
  return validationResults;
 };
```
