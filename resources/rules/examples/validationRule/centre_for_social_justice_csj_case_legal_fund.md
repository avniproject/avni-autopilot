---
rule_kind: validationRule
intent: lock the encounter date across edits and require at least one supporting media
  or file in the form
entity_param: programEncounter
encounter_types: []
concepts:
- 1f4bc226-9e3f-42c9-a332-2d81cd8e64f7
source_org: Centre for Social Justice (CSJ)
---
```js
"use strict";
 ({params, imports}) => {
  const programEncounter = params.entity;
  const validationResults = [];
  const formElement = params.formElement;
  const _ = imports.lodash;
  let validationErrors = [];
  const moment = imports.moment;
  
  let documentDetails = programEncounter.findGroupedObservation("c1cf21d1-5ce0-4246-ad0d-23137f8d8cdc");
  
  if(documentDetails && documentDetails.length > 0){
  for(let i = 0;i<documentDetails.length;i++){
  let currentQuestionGroup = documentDetails[i]
  let uploadFile = currentQuestionGroup.findObservationByConceptUUID("8eabb8c2-7708-471a-a0aa-c7dc549ad006");
  let uploadImage = currentQuestionGroup.findObservationByConceptUUID("fe30a8b6-0c79-4fb6-988a-4f2a717a8fbe");
 

  if((uploadFile === null || uploadFile === undefined) && (uploadImage === null || uploadImage === undefined)){
  let validationError = imports.common.createValidationError('Please upload either image or file');
  validationResults.push(validationError);
  }
  }
  }  
  
  const encounterDateForEditCase = programEncounter.getObservationValue("1f4bc226-9e3f-42c9-a332-2d81cd8e64f7");
  const encounterDate = programEncounter.encounterDateTime;
 

  if (!encounterDateForEditCase) {
  if (encounterDate) {
  const today = moment().startOf('day');
  const encDate = moment(encounterDate).startOf('day');
 

  if (!encDate.isSame(today, 'day')) {
  validationResults.push(
  imports.common.createValidationError("Encounter Date should be today's date.")
  );
  }
  }
  } else {
  const encDateEdit = moment(encounterDate).startOf('day');
  const encDateOriginal = moment(encounterDateForEditCase).startOf('day');
 

  if (!encDateEdit.isSame(encDateOriginal, 'day')) {
  validationResults.push(
  imports.common.createValidationError("Encounter Date cannot be changed.")
  );
  }
  }
  
  return validationResults;
 };
```
