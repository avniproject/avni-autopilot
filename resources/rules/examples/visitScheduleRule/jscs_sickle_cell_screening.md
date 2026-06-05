---
rule_kind: visitScheduleRule
intent: "schedule Base screening 3 months after the BT date if recorded; schedule Sample shipment on the encounter date based on solubility, electrophoresis, or old test result; schedule a Baseline visit on the encounter date if the old test result is Beta Thal, S-Beta thal, or SS — each only if not already scheduled"
entity_param: programEncounter
encounter_types: ["Base screening", "Baseline", "Sample shipment"]
concepts: ["BT date", "Electrophoresis result", "Result from old sickle cell test report", "Solubility result from field", "Whether BT done in last 3 months", "Whether old report available", "Whether prep and/or solubility result from field available"]
source_org: "JSCS"
---
```js
//SAMPLE RULE EXAMPLE
"use strict";
({params, imports}) => {
    const programEncounter = params.entity;
    const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({
        programEncounter
    });
    let btDate = programEncounter.getObservationReadableValue('BT date');
    
     const baseScreenEncounters = _.chain(programEncounter.programEnrolment.getEncounters(true))
        .filter((enc) => enc.name === 'Base screening')
        .filter((enc) => imports.moment(enc.earliestVisitDateTime).isSame(programEncounter.encounterDateTime) === true)
        .value();

    if (!_.isNil(btDate) && baseScreenEncounters.length == 0) {
        const dateAfter3MonthsOfBT = imports.moment(btDate).add(3, 'months').toDate();

        scheduleBuilder
            .add({
                name: "Base screening",
                encounterType: "Base screening",
                earliestDate: dateAfter3MonthsOfBT,
                maxDate: imports.moment(dateAfter3MonthsOfBT).add(3, 'days').toDate()
            });
    }

    let solResultField = programEncounter.getObservationReadableValue('Whether prep and/or solubility result from field available');
    let solResult = programEncounter.getObservationReadableValue('Solubility result from field');
    let btResult = programEncounter.getObservationReadableValue('Whether BT done in last 3 months');
    // if((_.isEqual(solResultField,'Yes') && _.isEqual(solResult,'Positive')) || _.isEqual(solResultField,'No') ){

   

    let elecResult = programEncounter.programEnrolment.getObservationReadableValueInEntireEnrolment('Electrophoresis result');
    let oldScResult = programEncounter.getObservationReadableValue('Solubility result from field');
    let oldReport = programEncounter.getObservationReadableValue('Whether old report available');
    var resultFromOldReport = programEncounter.getObservationReadableValue('Result from old sickle cell test report');

    
     const sampleShipEncounters = _.chain(programEncounter.programEnrolment.getEncounters(true))
        .filter((enc) => enc.name === 'Sample shipment')
        .filter((enc) => imports.moment(enc.earliestVisitDateTime).isSame(programEncounter.encounterDateTime) === true)
        .value();

    if (_.isEqual(btResult, 'No') && _.isEqual(oldReport, 'No') && sampleShipEncounters.length == 0) {
        if ((_.isEqual(solResultField, 'Yes') && _.isEqual(solResult, 'Positive')) || _.isEqual(solResultField, 'No')) {
            scheduleBuilder
                .add({
                    name: "Sample shipment",
                    encounterType: "Sample shipment",
                    earliestDate: imports.moment(programEncounter.encounterDateTime).toDate(),
                    maxDate: imports.moment(programEncounter.encounterDateTime).add(3, 'days').toDate()
                });
        }
    } else if (_.isEqual(btResult, 'No') &&  sampleShipEncounters.length == 0 && (_.isEqual(elecResult, 'Other') || _.isEqual(resultFromOldReport, 'Unconfirmed (Electrophoresis)'))) {
        scheduleBuilder
            .add({
                name: "Sample shipment",
                encounterType: "Sample shipment",
                earliestDate: imports.moment(programEncounter.encounterDateTime).toDate(),
                maxDate: imports.moment(programEncounter.encounterDateTime).add(3, 'days').toDate()
            });
    }

  const BaselineEncounters=programEncounter.programEnrolment.getEncountersOfType("Baseline")
               
    if(BaselineEncounters.length == 0){
        if(_.isEqual(resultFromOldReport,'Beta Thal') || _.isEqual(resultFromOldReport,'S-Beta thal') || _.isEqual(resultFromOldReport,'SS')){
        scheduleBuilder
        .add({
          name: "Baseline Form",
          encounterType: "Baseline",
          earliestDate: imports.moment(programEncounter.encounterDateTime).toDate(),
          maxDate: imports.moment(programEncounter.encounterDateTime).add(3, 'days').toDate()
        });
      }
    }

    return scheduleBuilder.getAllUnique("encounterType");
};
```
